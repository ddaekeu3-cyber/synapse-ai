---
layout: solution
title: "Agent Doesn't Implement Agent State Synchronization Protocol"
description: "How to keep state consistent across multiple instances of the same agent — handling concurrent updates, conflict resolution, and eventual consistency in distributed agent deployments."
tags: [general, distributed, state, synchronization, consistency, redis, crdt]
difficulty: advanced
solution_count: 6
---

## Problem

When an agent is deployed across multiple instances (load-balanced, auto-scaled, or geographically distributed), each instance maintains its own in-memory state. Instance A updates a conversation summary; instance B gets the next message and doesn't see the update. Preferences written by one instance are invisible to others. State diverges, producing inconsistent behavior for the same user across requests.

```python
# Bad: each instance has its own state — diverges immediately under load balancing
class AgentInstance:
    def __init__(self):
        self.memory = {}  # local only — invisible to other instances

instance_a = AgentInstance()
instance_b = AgentInstance()
instance_a.memory["user_preference"] = "dark mode"
# instance_b.memory["user_preference"] is empty — user sees inconsistency
```

---

## Solution 1 — Redis-Backed Shared State with Optimistic Locking

Use Redis as a shared state store. Use optimistic locking (WATCH + MULTI/EXEC) to detect concurrent modification and retry.

```python
import asyncio
import json
import time
from typing import Any
import redis.asyncio as aioredis

redis_client = aioredis.from_url("redis://localhost:6379", decode_responses=True)

STATE_KEY_PREFIX = "agent:state:"
STATE_TTL = 3600  # 1 hour

async def get_state(session_id: str) -> dict:
    raw = await redis_client.get(f"{STATE_KEY_PREFIX}{session_id}")
    return json.loads(raw) if raw else {}

async def set_state(session_id: str, state: dict) -> None:
    await redis_client.setex(
        f"{STATE_KEY_PREFIX}{session_id}",
        STATE_TTL,
        json.dumps(state),
    )

async def update_state_atomic(
    session_id: str,
    updates: dict,
    max_retries: int = 5,
) -> dict:
    """Optimistically update state; retry on concurrent modification."""
    key = f"{STATE_KEY_PREFIX}{session_id}"

    for attempt in range(max_retries):
        async with redis_client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                state = json.loads(raw) if raw else {}

                # Merge updates (last-write-wins per key)
                merged = {**state, **updates, "_updated_at": time.time()}

                pipe.multi()
                pipe.setex(key, STATE_TTL, json.dumps(merged))
                await pipe.execute()
                return merged
            except aioredis.WatchError:
                # Another instance modified the key — retry
                if attempt == max_retries - 1:
                    raise RuntimeError(
                        f"State update failed after {max_retries} retries (too much contention)"
                    )
                await asyncio.sleep(0.01 * (2 ** attempt))  # exponential backoff
    return {}

# Usage across multiple agent instances
async def agent_turn(session_id: str, message: str, instance_id: str) -> str:
    # Load shared state
    state = await get_state(session_id)
    conversation_history = state.get("history", [])
    conversation_history.append({"role": "user", "content": message})

    # Process (simplified)
    response = f"[{instance_id}] Response to: {message}"
    conversation_history.append({"role": "assistant", "content": response})

    # Write back atomically
    await update_state_atomic(session_id, {
        "history": conversation_history,
        "last_instance": instance_id,
    })
    return response
```

---

## Solution 2 — Event Sourcing: Append-Only Log as Source of Truth

Never mutate state directly. Append events to a shared log; derive current state by replaying the log. Multiple instances can write events concurrently without conflicts.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any
import redis.asyncio as aioredis

redis = aioredis.from_url("redis://localhost:6379", decode_responses=True)

@dataclass
class StateEvent:
    event_id: str
    session_id: str
    event_type: str   # "message_added", "preference_set", "tool_called", etc.
    payload: dict
    ts: float
    instance_id: str
    sequence: int = 0

EVENT_STREAM_KEY = "agent:events:{session_id}"

async def append_event(session_id: str, event_type: str,
                       payload: dict, instance_id: str) -> StateEvent:
    key = EVENT_STREAM_KEY.format(session_id=session_id)
    seq = await redis.xlen(key)
    event = StateEvent(
        event_id=str(uuid.uuid4()),
        session_id=session_id,
        event_type=event_type,
        payload=payload,
        ts=time.time(),
        instance_id=instance_id,
        sequence=seq,
    )
    await redis.xadd(key, asdict(event), maxlen=1000)
    return event

async def rebuild_state(session_id: str, since_sequence: int = 0) -> dict:
    """Replay all events to rebuild current state."""
    key = EVENT_STREAM_KEY.format(session_id=session_id)
    entries = await redis.xrange(key)
    state = {"history": [], "preferences": {}, "tool_results": {}, "version": 0}

    for entry_id, data in entries:
        seq = int(data.get("sequence", 0))
        if seq < since_sequence:
            continue
        event_type = data["event_type"]
        payload = json.loads(data["payload"]) if isinstance(data["payload"], str) else data["payload"]

        if event_type == "message_added":
            state["history"].append(payload)
        elif event_type == "preference_set":
            state["preferences"][payload["key"]] = payload["value"]
        elif event_type == "tool_called":
            state["tool_results"][payload["tool"]] = payload["result"]

        state["version"] = seq

    return state

async def subscribe_to_state_changes(session_id: str,
                                     callback: callable,
                                     poll_interval: float = 0.1) -> None:
    """Stream new events to this instance in near-real-time."""
    key = EVENT_STREAM_KEY.format(session_id=session_id)
    last_id = "$"  # only new events
    while True:
        entries = await redis.xread({key: last_id}, block=int(poll_interval * 1000), count=10)
        for _, events in entries:
            for entry_id, data in events:
                await callback(data)
                last_id = entry_id

# Usage
async def agent_a(session_id: str) -> None:
    await append_event(session_id, "message_added",
                       {"role": "user", "content": "Hello"}, "instance-a")
    await append_event(session_id, "preference_set",
                       {"key": "theme", "value": "dark"}, "instance-a")

async def agent_b(session_id: str) -> str:
    # Gets the full current state including instance-a's events
    state = await rebuild_state(session_id)
    return f"History has {len(state['history'])} messages, theme={state['preferences'].get('theme')}"

async def demo():
    sid = str(uuid.uuid4())
    await agent_a(sid)
    result = await agent_b(sid)
    print(result)  # History has 1 messages, theme=dark

asyncio.run(demo())
```

---

## Solution 3 — CRDT-Based Conflict-Free State Merging

Use Conflict-free Replicated Data Types (CRDTs) for state fields that multiple instances update concurrently. Merges are always deterministic without coordination.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
import redis.asyncio as aioredis

redis = aioredis.from_url("redis://localhost:6379", decode_responses=True)

# Last-Write-Wins Register (LWW-Register): simplest CRDT
@dataclass
class LWWRegister:
    """Last Write Wins: higher timestamp wins."""
    value: Any
    timestamp: float
    instance_id: str

    def merge(self, other: "LWWRegister") -> "LWWRegister":
        if other.timestamp > self.timestamp:
            return other
        if other.timestamp == self.timestamp and other.instance_id > self.instance_id:
            return other  # tie-break by instance ID
        return self

# Grow-Only Counter (G-Counter): each instance has its own slot
@dataclass
class GCounter:
    """Grow-only counter: merge by taking max of each instance's count."""
    counts: dict[str, int] = field(default_factory=dict)

    def increment(self, instance_id: str, amount: int = 1) -> None:
        self.counts[instance_id] = self.counts.get(instance_id, 0) + amount

    def value(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: "GCounter") -> "GCounter":
        merged = dict(self.counts)
        for inst, count in other.counts.items():
            merged[inst] = max(merged.get(inst, 0), count)
        return GCounter(merged)

@dataclass
class AgentCRDTState:
    """Agent state using CRDTs for conflict-free merging."""
    # LWW registers for single-value fields
    last_topic: LWWRegister = field(default_factory=lambda: LWWRegister("", 0.0, ""))
    user_name: LWWRegister = field(default_factory=lambda: LWWRegister("", 0.0, ""))
    # G-counter for message count
    message_count: GCounter = field(default_factory=GCounter)

    def merge(self, other: "AgentCRDTState") -> "AgentCRDTState":
        return AgentCRDTState(
            last_topic=self.last_topic.merge(other.last_topic),
            user_name=self.user_name.merge(other.user_name),
            message_count=self.message_count.merge(other.message_count),
        )

    def to_json(self) -> str:
        return json.dumps({
            "last_topic": {"value": self.last_topic.value,
                          "ts": self.last_topic.timestamp,
                          "inst": self.last_topic.instance_id},
            "user_name": {"value": self.user_name.value,
                         "ts": self.user_name.timestamp,
                         "inst": self.user_name.instance_id},
            "message_count": {"counts": self.message_count.counts},
        })

    @classmethod
    def from_json(cls, raw: str) -> "AgentCRDTState":
        d = json.loads(raw)
        return cls(
            last_topic=LWWRegister(d["last_topic"]["value"], d["last_topic"]["ts"], d["last_topic"]["inst"]),
            user_name=LWWRegister(d["user_name"]["value"], d["user_name"]["ts"], d["user_name"]["inst"]),
            message_count=GCounter(d["message_count"]["counts"]),
        )

STATE_KEY = "agent:crdt:{session_id}"

async def load_and_merge(session_id: str, local: AgentCRDTState) -> AgentCRDTState:
    key = STATE_KEY.format(session_id=session_id)
    raw = await redis.get(key)
    if raw:
        remote = AgentCRDTState.from_json(raw)
        merged = local.merge(remote)
    else:
        merged = local
    await redis.setex(key, 3600, merged.to_json())
    return merged

# Usage: two instances update concurrently — merge is conflict-free
async def instance_a(session_id: str) -> None:
    state = AgentCRDTState()
    state.last_topic = LWWRegister("machine learning", time.time(), "inst-a")
    state.message_count.increment("inst-a")
    await load_and_merge(session_id, state)

async def instance_b(session_id: str) -> None:
    state = AgentCRDTState()
    state.last_topic = LWWRegister("quantum computing", time.time() + 1, "inst-b")
    state.message_count.increment("inst-b", 2)
    merged = await load_and_merge(session_id, state)
    print(f"topic={merged.last_topic.value}, messages={merged.message_count.value()}")
    # topic=quantum computing (later timestamp wins), messages=3

async def demo():
    sid = str(uuid.uuid4())
    await asyncio.gather(instance_a(sid), instance_b(sid))

asyncio.run(demo())
```

---

## Solution 4 — Gossip Protocol for Agent State Propagation

Agents periodically gossip their state to a random peer. Over several rounds, all instances converge to the same state — tolerant of network partitions and instance failures.

```python
import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class GossipState:
    instance_id: str
    state: dict
    vector_clock: dict[str, int] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def increment_clock(self) -> None:
        self.vector_clock[self.instance_id] = (
            self.vector_clock.get(self.instance_id, 0) + 1
        )
        self.ts = time.time()

    def merge_clocks(self, other_clock: dict[str, int]) -> None:
        for inst, count in other_clock.items():
            self.vector_clock[inst] = max(self.vector_clock.get(inst, 0), count)

    def is_newer_than(self, other: "GossipState") -> bool:
        for inst, count in other.vector_clock.items():
            if self.vector_clock.get(inst, 0) < count:
                return False
        return True

class GossipNode:
    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self._state = GossipState(instance_id, {})
        self._peers: list["GossipNode"] = []

    def add_peer(self, peer: "GossipNode") -> None:
        self._peers.append(peer)

    def update_local(self, key: str, value: Any) -> None:
        self._state.state[key] = value
        self._state.increment_clock()

    def get(self, key: str) -> Any:
        return self._state.state.get(key)

    def receive_gossip(self, incoming: GossipState) -> bool:
        """Merge incoming state. Returns True if local state was updated."""
        updated = False
        for key, value in incoming.state.items():
            if key not in self._state.state:
                self._state.state[key] = value
                updated = True
            # Last-write-wins by timestamp for simplicity
            # In production: use vector clocks or CRDTs per key

        self._state.merge_clocks(incoming.vector_clock)
        return updated

    async def gossip_round(self) -> None:
        if not self._peers:
            return
        target = random.choice(self._peers)
        target.receive_gossip(self._state)

    async def run_gossip(self, interval: float = 0.5) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.gossip_round()

# Simulation
async def gossip_demo():
    nodes = [GossipNode(f"inst-{i}") for i in range(5)]
    for node in nodes:
        node._peers = [p for p in nodes if p is not node]

    # Start gossip loops
    gossip_tasks = [asyncio.create_task(node.run_gossip(0.1)) for node in nodes]

    # Inst-0 writes a value
    nodes[0].update_local("user_preference", "dark_mode")
    nodes[0].update_local("session_goal", "write a poem")

    # Wait for gossip to propagate
    await asyncio.sleep(1.0)

    for node in nodes:
        pref = node.get("user_preference")
        goal = node.get("session_goal")
        print(f"{node.instance_id}: preference={pref}, goal={goal}")

    for task in gossip_tasks:
        task.cancel()

asyncio.run(gossip_demo())
# After 1s all nodes have: preference=dark_mode, goal=write a poem
```

---

## Solution 5 — Leader Election for State Ownership

Elect one instance as the authoritative state owner per session. Other instances proxy state reads/writes through the leader. On leader failure, elect a new one.

```python
import asyncio
import time
import uuid
import redis.asyncio as aioredis

redis = aioredis.from_url("redis://localhost:6379", decode_responses=True)

LEADER_KEY_PREFIX = "agent:leader:"
LEADER_TTL = 10  # seconds

class LeaderElector:
    def __init__(self, instance_id: str, session_id: str):
        self.instance_id = instance_id
        self.session_id = session_id
        self._is_leader = False
        self._key = f"{LEADER_KEY_PREFIX}{session_id}"

    async def try_become_leader(self) -> bool:
        """Attempt to acquire leadership. Returns True if successful."""
        # NX = only set if not exists; EX = expiry in seconds
        acquired = await redis.set(
            self._key, self.instance_id, nx=True, ex=LEADER_TTL
        )
        self._is_leader = bool(acquired)
        return self._is_leader

    async def get_leader(self) -> str | None:
        return await redis.get(self._key)

    async def renew_lease(self) -> bool:
        """Renew leadership lease. Returns False if lease was stolen."""
        if not self._is_leader:
            return False
        current = await redis.get(self._key)
        if current != self.instance_id:
            self._is_leader = False
            return False
        await redis.expire(self._key, LEADER_TTL)
        return True

    async def release_leadership(self) -> None:
        if self._is_leader:
            current = await redis.get(self._key)
            if current == self.instance_id:
                await redis.delete(self._key)
            self._is_leader = False

    async def run_renewal_loop(self, interval: float = 3.0) -> None:
        while True:
            await asyncio.sleep(interval)
            renewed = await self.renew_lease()
            if not renewed:
                print(f"[{self.instance_id}] Lost leadership of session {self.session_id}")
                # Attempt to re-acquire
                await self.try_become_leader()

class LeaderGatedStateManager:
    def __init__(self, instance_id: str, session_id: str):
        self._elector = LeaderElector(instance_id, session_id)
        self._local_state: dict = {}
        self.instance_id = instance_id

    async def write(self, key: str, value) -> bool:
        """Write succeeds only on leader; followers reject."""
        if not self._elector._is_leader:
            leader = await self._elector.get_leader()
            print(f"[{self.instance_id}] Not leader (leader={leader}) — proxying write")
            # In production: forward the write to the leader via HTTP/RPC
            return False
        self._local_state[key] = value
        # Also persist to Redis so followers can read
        await redis.hset(f"agent:state:{self._elector.session_id}", key, str(value))
        return True

    async def read(self, key: str):
        """All instances can read from Redis."""
        return await redis.hget(f"agent:state:{self._elector.session_id}", key)

async def demo():
    instances = [
        LeaderGatedStateManager(f"inst-{i}", "session-xyz")
        for i in range(3)
    ]
    # Elect leader
    for inst in instances:
        won = await inst._elector.try_become_leader()
        print(f"{inst.instance_id}: leader={won}")

    leader = next((i for i in instances if i._elector._is_leader), None)
    if leader:
        await leader.write("conversation_goal", "help user learn Python")

    for inst in instances:
        val = await inst.read("conversation_goal")
        print(f"{inst.instance_id} reads: {val}")

asyncio.run(demo())
```

---

## Solution 6 — Snapshot + Delta Sync Protocol

Each instance periodically publishes a full state snapshot. On startup or after a gap, instances download the latest snapshot. Between snapshots, only deltas (changed keys) are propagated.

```python
import asyncio
import json
import time
import uuid
import gzip
import base64
import redis.asyncio as aioredis

redis = aioredis.from_url("redis://localhost:6379", decode_responses=True)

SNAPSHOT_KEY = "agent:snapshot:{session_id}"
DELTA_CHANNEL = "agent:deltas:{session_id}"

def compress(data: dict) -> str:
    raw = json.dumps(data).encode()
    return base64.b64encode(gzip.compress(raw)).decode()

def decompress(s: str) -> dict:
    raw = gzip.decompress(base64.b64decode(s.encode()))
    return json.loads(raw)

class SnapshotDeltaSyncManager:
    def __init__(self, instance_id: str, session_id: str,
                 snapshot_interval: float = 30.0):
        self.instance_id = instance_id
        self.session_id = session_id
        self._snapshot_interval = snapshot_interval
        self._state: dict = {}
        self._dirty: dict = {}  # changed since last snapshot
        self._snapshot_key = SNAPSHOT_KEY.format(session_id=session_id)
        self._delta_channel = DELTA_CHANNEL.format(session_id=session_id)

    async def restore_from_snapshot(self) -> None:
        raw = await redis.get(self._snapshot_key)
        if raw:
            self._state = decompress(raw)
            print(f"[{self.instance_id}] Restored {len(self._state)} keys from snapshot")

    def set(self, key: str, value) -> None:
        self._state[key] = value
        self._dirty[key] = value

    def get(self, key: str):
        return self._state.get(key)

    async def publish_delta(self) -> None:
        if not self._dirty:
            return
        delta = {
            "instance_id": self.instance_id,
            "ts": time.time(),
            "changes": self._dirty,
        }
        await redis.publish(self._delta_channel, json.dumps(delta))
        self._dirty = {}

    async def publish_snapshot(self) -> None:
        compressed = compress(self._state)
        await redis.setex(self._snapshot_key, 86400, compressed)
        print(f"[{self.instance_id}] Published snapshot ({len(compressed)} chars)")

    async def apply_delta(self, raw_delta: str) -> None:
        delta = json.loads(raw_delta)
        if delta["instance_id"] == self.instance_id:
            return  # don't apply own delta
        self._state.update(delta["changes"])
        print(f"[{self.instance_id}] Applied delta from {delta['instance_id']}: {list(delta['changes'].keys())}")

    async def subscribe_to_deltas(self) -> None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(self._delta_channel)
        async for message in pubsub.listen():
            if message["type"] == "message":
                await self.apply_delta(message["data"])

    async def run_sync_loop(self) -> None:
        await self.restore_from_snapshot()
        asyncio.create_task(self.subscribe_to_deltas())
        while True:
            await asyncio.sleep(1.0)
            await self.publish_delta()
            if int(time.time()) % int(self._snapshot_interval) == 0:
                await self.publish_snapshot()
```

---

## Comparison

| Approach | Consistency | Conflict Resolution | Partition Tolerance | Latency | Best For |
|---|---|---|---|---|---|
| Redis optimistic lock | **Strong** | Retry on conflict | No | Low | Low-contention state |
| Event sourcing | **Strong** | **Append-only (no conflicts)** | Partial | Low | Audit-required systems |
| CRDT-based | Eventual | **Automatic (math-guaranteed)** | **Yes** | **Lowest** | High-contention counters/registers |
| Gossip protocol | Eventual | Last-write-wins | **Yes** | Variable | Peer-to-peer agent meshes |
| Leader election | **Strong** | No conflicts (single writer) | No | Medium | Single-authoritative-state |
| Snapshot + delta | Eventual | Last-write-wins | **Yes** | Low | Bandwidth-efficient large states |
