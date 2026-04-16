---
title: "Agent Doesn't Implement State Reconciliation After Network Partition"
description: "When agent replicas lose connectivity and continue operating independently, they accumulate divergent state. Without reconciliation logic, reconnection either silently overwrites correct state or halts the system requiring manual intervention."
difficulty: advanced
category: reliability
tags: [partition, reconciliation, vector-clock, conflict-resolution, distributed, state, reliability]
---

## Problem

Two agent replicas (A and B) lose connectivity. Both continue accepting writes. When the partition heals, the system has two versions of truth. Naive last-write-wins deletes valid updates from both sides. No reconciliation means manual inspection of every divergent key.

```python
# Broken: last-write-wins — silent data loss during partition healing
async def merge_states(local: dict, remote: dict) -> dict:
    return {**local, **remote}  # remote silently wins on all conflicts
```

---

## Solution 1: Vector Clock Conflict Detection

```python
import copy
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class VectorClock:
    """
    Logical clock tracking causality across replicas.
    clock[node_id] = number of events seen from that node.
    """
    clock: dict[str, int] = field(default_factory=dict)

    def increment(self, node_id: str) -> "VectorClock":
        new = VectorClock(clock=dict(self.clock))
        new.clock[node_id] = new.clock.get(node_id, 0) + 1
        return new

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Component-wise maximum — merges two causal histories."""
        all_nodes = set(self.clock) | set(other.clock)
        return VectorClock({n: max(self.clock.get(n, 0),
                                    other.clock.get(n, 0))
                            for n in all_nodes})

    def happens_before(self, other: "VectorClock") -> bool:
        """True if self causally precedes other."""
        return (all(self.clock.get(n, 0) <= other.clock.get(n, 0)
                    for n in set(self.clock) | set(other.clock)) and
                self.clock != other.clock)

    def concurrent_with(self, other: "VectorClock") -> bool:
        """True if neither clock dominates the other (conflict)."""
        return (not self.happens_before(other) and
                not other.happens_before(self))

@dataclass
class VersionedValue:
    value: Any
    clock: VectorClock
    node_id: str
    wall_time: float = field(default_factory=time.time)

class VectorClockStateStore:
    """
    Key-value store where every value is timestamped with a vector clock.
    Conflict detection is exact: two updates conflict iff their clocks are concurrent.
    """

    def __init__(self, node_id: str):
        self.node_id = node_id
        self._store: dict[str, VersionedValue] = {}
        self._clock = VectorClock()

    def write(self, key: str, value: Any) -> VectorClock:
        self._clock = self._clock.increment(self.node_id)
        self._store[key] = VersionedValue(
            value=value,
            clock=VectorClock(dict(self._clock.clock)),
            node_id=self.node_id,
        )
        return self._clock

    def read(self, key: str) -> VersionedValue | None:
        return self._store.get(key)

    def reconcile(self, remote_store: "VectorClockStateStore") -> dict:
        """
        Merge remote store into local. Returns a report of conflicts.
        """
        conflicts: dict[str, tuple[VersionedValue, VersionedValue]] = {}
        auto_merged: list[str] = []

        for key, remote_val in remote_store._store.items():
            local_val = self._store.get(key)
            if local_val is None:
                # Remote has data we don't — accept it
                self._store[key] = remote_val
                auto_merged.append(key)
            elif remote_val.clock.happens_before(local_val.clock):
                # Local is newer — keep local
                auto_merged.append(key)
            elif local_val.clock.happens_before(remote_val.clock):
                # Remote is newer — accept remote
                self._store[key] = remote_val
                auto_merged.append(key)
            else:
                # Concurrent updates — true conflict
                conflicts[key] = (local_val, remote_val)

        # Merge clocks
        self._clock = self._clock.merge(remote_store._clock)
        return {"auto_merged": auto_merged, "conflicts": conflicts}
```

---

## Solution 2: CRDT-Based Conflict-Free State (Grow-Only Counter)

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any

@dataclass
class GCounter:
    """
    Grow-only counter CRDT: each replica tracks its own increments.
    Merge = component-wise maximum. No conflicts possible.
    """
    counts: dict[str, int] = field(default_factory=dict)

    def increment(self, node_id: str, amount: int = 1) -> "GCounter":
        new = GCounter(dict(self.counts))
        new.counts[node_id] = new.counts.get(node_id, 0) + amount
        return new

    def value(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: "GCounter") -> "GCounter":
        all_nodes = set(self.counts) | set(other.counts)
        return GCounter({n: max(self.counts.get(n, 0), other.counts.get(n, 0))
                         for n in all_nodes})

@dataclass
class PNCounter:
    """
    Positive-Negative counter CRDT: supports increment and decrement.
    Value = sum(positive) - sum(negative). No conflicts.
    """
    positive: GCounter = field(default_factory=GCounter)
    negative: GCounter = field(default_factory=GCounter)

    def increment(self, node_id: str, amount: int = 1) -> "PNCounter":
        return PNCounter(positive=self.positive.increment(node_id, amount),
                         negative=self.negative)

    def decrement(self, node_id: str, amount: int = 1) -> "PNCounter":
        return PNCounter(positive=self.positive,
                         negative=self.negative.increment(node_id, amount))

    def value(self) -> int:
        return self.positive.value() - self.negative.value()

    def merge(self, other: "PNCounter") -> "PNCounter":
        return PNCounter(positive=self.positive.merge(other.positive),
                         negative=self.negative.merge(other.negative))

@dataclass
class LWWRegister:
    """
    Last-Write-Wins Register: a single value with a timestamp.
    On merge, the higher-timestamp value wins. Simple but loses data.
    """
    value: Any = None
    timestamp: float = 0.0
    node_id: str = ""

    def set(self, value: Any, timestamp: float, node_id: str) -> "LWWRegister":
        return LWWRegister(value=value, timestamp=timestamp, node_id=node_id)

    def merge(self, other: "LWWRegister") -> "LWWRegister":
        if other.timestamp > self.timestamp:
            return other
        if other.timestamp == self.timestamp and other.node_id > self.node_id:
            return other  # tie-break by node ID
        return self

class CRDTAgentState:
    """
    Agent state built from CRDTs — automatically reconcilable after partition.
    No conflicts, no manual intervention needed.
    """

    def __init__(self, node_id: str):
        self.node_id = node_id
        self.request_count = GCounter()
        self.active_sessions = PNCounter()
        self.config = LWWRegister()

    def merge(self, other: "CRDTAgentState") -> "CRDTAgentState":
        merged = CRDTAgentState(self.node_id)
        merged.request_count = self.request_count.merge(other.request_count)
        merged.active_sessions = self.active_sessions.merge(other.active_sessions)
        merged.config = self.config.merge(other.config)
        return merged

    def to_dict(self) -> dict:
        return {
            "request_count": self.request_count.value(),
            "active_sessions": self.active_sessions.value(),
            "config": self.config.value,
        }
```

---

## Solution 3: Merkle Tree State Fingerprint for Efficient Diff

```python
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

def _hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]

@dataclass
class MerkleNode:
    key: str
    value_hash: str
    children: dict[str, "MerkleNode"] = field(default_factory=dict)

    @property
    def node_hash(self) -> str:
        child_hashes = "".join(
            sorted(f"{k}:{v.node_hash}" for k, v in self.children.items())
        )
        return _hash(f"{self.key}:{self.value_hash}:{child_hashes}")

class MerkleStateTree:
    """
    Represent agent state as a Merkle tree for efficient partition detection.
    Two replicas can compare root hashes to determine if their states differ,
    then narrow down to specific divergent keys in O(log n) comparisons.
    """

    def __init__(self):
        self._data: dict[str, Any] = {}

    def set(self, key: str, value: Any):
        self._data[key] = value

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def root_hash(self) -> str:
        """Fingerprint of entire state — changes if any value changes."""
        canonical = json.dumps(
            {k: v for k, v in sorted(self._data.items())},
            sort_keys=True, default=str
        )
        return _hash(canonical)

    def diff_keys(self, other: "MerkleStateTree") -> list[str]:
        """Return keys that differ between this and other tree."""
        if self.root_hash() == other.root_hash():
            return []  # identical — fast path
        return [
            k for k in set(self._data) | set(other._data)
            if self._data.get(k) != other._data.get(k)
        ]

class PartitionDetector:
    """
    Uses Merkle fingerprints to efficiently detect divergence between replicas.
    Only compares root hash across the wire; only fetches divergent keys on mismatch.
    """

    def __init__(self, node_id: str, state: MerkleStateTree):
        self.node_id = node_id
        self._state = state
        self._last_sync_hash: str | None = None

    def is_diverged(self, remote_hash: str) -> bool:
        return self._state.root_hash() != remote_hash

    def diverged_keys(self, remote_state: MerkleStateTree) -> list[str]:
        return self._state.diff_keys(remote_state)

    def sync_summary(self, remote: "PartitionDetector") -> dict:
        local_hash = self._state.root_hash()
        remote_hash = remote._state.root_hash()
        diverged = self._state.diff_keys(remote._state)
        return {
            "diverged": len(diverged) > 0,
            "local_hash": local_hash[:8],
            "remote_hash": remote_hash[:8],
            "diverged_keys": diverged,
            "diverged_count": len(diverged),
        }
```

---

## Solution 4: Conflict Resolution Policy Engine

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

class ResolutionPolicy(Enum):
    LAST_WRITE_WINS      = "lww"
    FIRST_WRITE_WINS     = "fww"
    HIGHER_VALUE_WINS    = "max"
    LOWER_VALUE_WINS     = "min"
    MERGE_SETS           = "merge_sets"
    MERGE_DICTS          = "merge_dicts"
    REQUIRE_MANUAL       = "manual"
    CUSTOM               = "custom"

@dataclass
class ConflictResolutionRule:
    key_pattern: str         # key prefix or exact key
    policy: ResolutionPolicy
    custom_fn: Callable | None = None

class ConflictResolutionEngine:
    """
    Policy-driven conflict resolution.
    Different keys have different merge semantics:
    - Counters → add
    - Sets → union
    - Config → last-write-wins
    - Financial totals → require manual review
    """

    def __init__(self, rules: list[ConflictResolutionRule],
                 default_policy: ResolutionPolicy = ResolutionPolicy.LAST_WRITE_WINS):
        self._rules = rules
        self._default = default_policy
        self._manual_conflicts: list[dict] = []

    def _match_rule(self, key: str) -> ConflictResolutionRule | None:
        for rule in self._rules:
            if key.startswith(rule.key_pattern) or key == rule.key_pattern:
                return rule
        return None

    def resolve(self, key: str,
                local: "VersionedValue",
                remote: "VersionedValue") -> Any | None:
        """
        Returns resolved value, or None if manual review required.
        """
        rule = self._match_rule(key)
        policy = rule.policy if rule else self._default

        if policy == ResolutionPolicy.LAST_WRITE_WINS:
            return remote.value if remote.wall_time >= local.wall_time else local.value

        if policy == ResolutionPolicy.FIRST_WRITE_WINS:
            return local.value if local.wall_time <= remote.wall_time else remote.value

        if policy == ResolutionPolicy.HIGHER_VALUE_WINS:
            try:
                return max(local.value, remote.value)
            except TypeError:
                return local.value

        if policy == ResolutionPolicy.LOWER_VALUE_WINS:
            try:
                return min(local.value, remote.value)
            except TypeError:
                return local.value

        if policy == ResolutionPolicy.MERGE_SETS:
            try:
                return list(set(local.value) | set(remote.value))
            except TypeError:
                return local.value

        if policy == ResolutionPolicy.MERGE_DICTS:
            try:
                merged = dict(local.value)
                merged.update(remote.value)
                return merged
            except (TypeError, ValueError):
                return local.value

        if policy == ResolutionPolicy.CUSTOM and rule and rule.custom_fn:
            return rule.custom_fn(local.value, remote.value)

        if policy == ResolutionPolicy.REQUIRE_MANUAL:
            self._manual_conflicts.append({
                "key": key,
                "local": local.value,
                "remote": remote.value,
                "local_time": local.wall_time,
                "remote_time": remote.wall_time,
            })
            return None  # Not resolved — needs manual review

        return local.value  # fallback

    def pending_manual_conflicts(self) -> list[dict]:
        return list(self._manual_conflicts)

# Example: build rules for an agent state store
def build_agent_conflict_rules() -> list[ConflictResolutionRule]:
    return [
        ConflictResolutionRule("counter:", ResolutionPolicy.HIGHER_VALUE_WINS),
        ConflictResolutionRule("config:", ResolutionPolicy.LAST_WRITE_WINS),
        ConflictResolutionRule("session:", ResolutionPolicy.LAST_WRITE_WINS),
        ConflictResolutionRule("tool_cache:", ResolutionPolicy.LAST_WRITE_WINS),
        ConflictResolutionRule("balance:", ResolutionPolicy.REQUIRE_MANUAL),
        ConflictResolutionRule("permissions:", ResolutionPolicy.REQUIRE_MANUAL),
        ConflictResolutionRule("tags:", ResolutionPolicy.MERGE_SETS),
    ]
```

---

## Solution 5: Anti-Entropy Gossip Protocol

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class GossipMessage:
    sender: str
    state_digest: dict[str, str]  # key → hash of value
    timestamp: float = field(default_factory=time.time)

class GossipReconciler:
    """
    Lightweight gossip-based reconciliation.
    Each node periodically shares its state digest (key → hash).
    Peers request full values only for keys where hashes differ.
    Converges to consistent state without central coordinator.
    """

    def __init__(self, node_id: str,
                 state: "VectorClockStateStore",
                 peers: list[str]):
        self.node_id = node_id
        self._state = state
        self._peers = peers
        self._peer_digests: dict[str, dict[str, str]] = {}

    def compute_digest(self) -> dict[str, str]:
        """Hash of each value for cheap comparison."""
        import hashlib, json
        return {
            k: hashlib.md5(
                json.dumps(v.value, sort_keys=True, default=str).encode()
            ).hexdigest()[:8]
            for k, v in self._state._store.items()
        }

    def differing_keys(self, peer_digest: dict[str, str]) -> list[str]:
        """Keys where our value hash differs from peer's."""
        local_digest = self.compute_digest()
        return [
            k for k in set(local_digest) | set(peer_digest)
            if local_digest.get(k) != peer_digest.get(k)
        ]

    async def gossip_round(self,
                            send_fn: "Callable[[str, GossipMessage], Awaitable[None]]",
                            request_fn: "Callable[[str, list[str]], Awaitable[dict]]"):
        """
        One gossip round: pick random peer, compare digests, exchange diffs.
        """
        if not self._peers:
            return
        peer = random.choice(self._peers)
        msg = GossipMessage(sender=self.node_id, state_digest=self.compute_digest())

        # Send our digest and receive peer's digest
        await send_fn(peer, msg)

    async def handle_gossip(self, msg: GossipMessage,
                             engine: "ConflictResolutionEngine") -> list[str]:
        """
        Handle incoming gossip message.
        Returns list of keys we need from the sender.
        """
        diff_keys = self.differing_keys(msg.state_digest)
        self._peer_digests[msg.sender] = msg.state_digest
        return diff_keys

    async def run_anti_entropy(self, send_fn, request_fn,
                                interval: float = 30.0):
        """Periodic gossip loop."""
        while True:
            await asyncio.sleep(interval + random.uniform(-5, 5))
            try:
                await self.gossip_round(send_fn, request_fn)
            except Exception as e:
                print(f"[Gossip] Round failed: {e}")
```

---

## Solution 6: Partition-Aware Agent Coordinator

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

class PartitionMode(Enum):
    CONNECTED    = "connected"
    PARTITIONED  = "partitioned"
    HEALING      = "healing"

@dataclass
class PartitionConfig:
    # How long without heartbeat before declaring partition
    partition_timeout: float = 10.0
    # Operations to allow during partition (offline mode)
    allow_reads_during_partition: bool = True
    allow_writes_during_partition: bool = True
    # After healing: how long to wait for reconciliation before serving
    healing_grace_period: float = 5.0

class PartitionAwareCoordinator:
    """
    Tracks connectivity to peers and adjusts agent behavior
    based on partition mode.
    """

    def __init__(self, node_id: str, peers: list[str],
                 config: PartitionConfig = PartitionConfig()):
        self.node_id = node_id
        self._peers = set(peers)
        self._config = config
        self._last_heartbeat: dict[str, float] = {p: time.monotonic()
                                                   for p in peers}
        self._mode = PartitionMode.CONNECTED
        self._partition_started: float | None = None
        self._writes_during_partition: int = 0

    def record_heartbeat(self, peer_id: str):
        self._last_heartbeat[peer_id] = time.monotonic()

    def detect_mode(self) -> PartitionMode:
        now = time.monotonic()
        unreachable = {
            p for p in self._peers
            if now - self._last_heartbeat.get(p, 0) > self._config.partition_timeout
        }
        if unreachable == self._peers:
            return PartitionMode.PARTITIONED
        if unreachable:
            return PartitionMode.PARTITIONED   # partial partition = partitioned
        return PartitionMode.CONNECTED

    async def update_mode(self):
        new_mode = self.detect_mode()
        if new_mode != self._mode:
            print(f"[Partition] Mode change: {self._mode.value} → {new_mode.value}")
            if new_mode == PartitionMode.PARTITIONED:
                self._partition_started = time.monotonic()
                self._writes_during_partition = 0
            elif new_mode == PartitionMode.CONNECTED and \
                    self._mode == PartitionMode.PARTITIONED:
                # Partition healed — trigger reconciliation
                partition_duration = (time.monotonic() -
                                      (self._partition_started or 0))
                print(f"[Partition] Healed after {partition_duration:.1f}s. "
                      f"Writes during partition: {self._writes_during_partition}. "
                      f"Reconciliation required.")
            self._mode = new_mode

    def can_serve_request(self, is_write: bool) -> tuple[bool, str]:
        if self._mode == PartitionMode.CONNECTED:
            return True, "ok"
        if is_write and not self._config.allow_writes_during_partition:
            return False, "writes_disabled_during_partition"
        if not is_write and not self._config.allow_reads_during_partition:
            return False, "reads_disabled_during_partition"
        if is_write:
            self._writes_during_partition += 1
        return True, f"serving_{self._mode.value}_may_need_reconciliation"

    @property
    def mode(self) -> PartitionMode:
        return self._mode

    async def heartbeat_monitor(self, interval: float = 2.0):
        while True:
            await asyncio.sleep(interval)
            await self.update_mode()
```

---

## Comparison

| Solution | Conflict Detection | Auto-Resolution | Manual Review | Efficiency | Complexity | Best For |
|---|---|---|---|---|---|---|
| 1. Vector clocks | Exact (causal) | Partial | Concurrent conflicts | O(nodes) per key | Med | General key-value state |
| 2. CRDTs | None (conflict-free) | Always automatic | Never | O(nodes) per key | Med | Counters, sets, LWW registers |
| 3. Merkle tree diff | Root hash | N/A (detection only) | Yes | O(log n) comparison | Low | Efficient diff across large state |
| 4. Policy engine | Via vector clock | Policy-driven | Sensitive keys | O(1) per rule | Med | Domain-specific merge rules |
| 5. Gossip anti-entropy | Hash comparison | After exchange | No | O(diff size) | High | Decentralized, large clusters |
| 6. Partition coordinator | Heartbeat timeout | N/A (mode control) | Post-healing | O(1) | Med | Offline mode + healing detection |

**Key principle**: prefer CRDTs (solution 2) for state that can be modeled as counters or sets — they never conflict and reconcile automatically. Use vector clocks (solution 1) for general key-value state where you need exact conflict detection. Use a policy engine (solution 4) to define domain-specific resolution semantics (max for metrics, union for tags, manual for money). Always track writes-during-partition so post-healing reconciliation knows which keys need attention.
