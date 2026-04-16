---
title: "Agent Doesn't Implement Consistent Hashing for Work Distribution"
description: "How to use consistent hashing to distribute work evenly across agent instances with minimal remapping when the cluster size changes — enabling stateful session affinity, cache locality, and balanced load without a central scheduler."
date: 2025-01-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-consistent-hashing-for-work-distribution
tags:
  - reliability
  - consistent-hashing
  - load-balancing
  - work-distribution
  - session-affinity
  - scalability
  - cluster
symptoms:
  - "Adding or removing an agent instance causes all work to be reshuffled, breaking in-flight sessions"
  - "Session-based work is routed to random instances losing in-memory state"
  - "Naive round-robin load balancing doesn't account for instance failures or scale-out events"
  - "Cache hit rates drop to near zero after any cluster topology change"
  - "No deterministic mapping from user/session ID to agent instance"
  - "Long-running tasks migrate to wrong instances when the cluster changes"
---

## Why This Happens

When agent workloads require session affinity — routing the same user's requests to the same agent instance — or when in-memory caches should be co-located with the work they serve, naive load-balancing strategies break down. Modular hashing (`user_id % N`) requires reshuffling O(N) work items when a node is added or removed, causing session loss and cache invalidation at scale.

Consistent hashing solves this: it arranges nodes on a virtual ring, and each key maps to the nearest node clockwise. When a node is added or removed, only O(K/N) keys need remapping (where K is the total number of keys and N is the node count) — dramatically reducing disruption during scale events.

---

## Solution 1: Basic Consistent Hash Ring

A hash ring implementation using virtual nodes for uniform distribution.

```python
import hashlib
import bisect
from dataclasses import dataclass
from typing import Optional

@dataclass
class RingNode:
    node_id: str
    address: str
    weight: int = 100  # Number of virtual nodes (higher = more load)

class ConsistentHashRing:
    """
    Consistent hash ring with virtual nodes for uniform distribution.
    Virtual nodes improve balance: each physical node has `weight` positions on the ring.
    """

    def __init__(self, hash_fn=None):
        self._hash_fn = hash_fn or self._md5_hash
        self._ring: dict[int, RingNode] = {}    # position -> node
        self._sorted_keys: list[int] = []       # sorted ring positions
        self._nodes: dict[str, RingNode] = {}   # node_id -> node

    @staticmethod
    def _md5_hash(key: str) -> int:
        return int(hashlib.md5(key.encode()).hexdigest(), 16)

    def _virtual_key(self, node_id: str, replica: int) -> str:
        return f"{node_id}#vnode{replica}"

    def add_node(self, node: RingNode) -> None:
        """Add a node with `weight` virtual positions on the ring."""
        self._nodes[node.node_id] = node
        for i in range(node.weight):
            position = self._hash_fn(self._virtual_key(node.node_id, i))
            self._ring[position] = node
        self._sorted_keys = sorted(self._ring.keys())

    def remove_node(self, node_id: str) -> None:
        """Remove a node and all its virtual positions."""
        node = self._nodes.pop(node_id, None)
        if node is None:
            return
        for i in range(node.weight):
            position = self._hash_fn(self._virtual_key(node_id, i))
            self._ring.pop(position, None)
        self._sorted_keys = sorted(self._ring.keys())

    def get_node(self, key: str) -> Optional[RingNode]:
        """Find the node responsible for this key."""
        if not self._ring:
            return None
        position = self._hash_fn(key)
        idx = bisect.bisect_right(self._sorted_keys, position)
        if idx == len(self._sorted_keys):
            idx = 0  # Wrap around
        return self._ring[self._sorted_keys[idx]]

    def get_nodes(self, key: str, count: int = 1) -> list[RingNode]:
        """Get `count` distinct nodes for replication."""
        if not self._ring:
            return []
        position = self._hash_fn(key)
        idx = bisect.bisect_right(self._sorted_keys, position)

        seen_node_ids: set[str] = set()
        result: list[RingNode] = []

        for i in range(len(self._sorted_keys)):
            ring_idx = (idx + i) % len(self._sorted_keys)
            node = self._ring[self._sorted_keys[ring_idx]]
            if node.node_id not in seen_node_ids:
                seen_node_ids.add(node.node_id)
                result.append(node)
            if len(result) == count:
                break
        return result

    def distribution(self) -> dict[str, int]:
        """Show how many virtual positions each node owns."""
        counts: dict[str, int] = {}
        for node in self._ring.values():
            counts[node.node_id] = counts.get(node.node_id, 0) + 1
        return counts

    def all_nodes(self) -> list[RingNode]:
        return list(self._nodes.values())


# --- Usage ---

def demo_ring():
    ring = ConsistentHashRing()
    for i in range(3):
        ring.add_node(RingNode(f"agent-{i}", f"10.0.0.{i}:8080", weight=150))

    # Route requests deterministically
    for user_id in ["alice", "bob", "charlie", "diana"]:
        node = ring.get_node(f"session:{user_id}")
        print(f"User {user_id} -> {node.node_id}")

    # Add a new agent — only ~1/4 of keys remapped
    ring.add_node(RingNode("agent-3", "10.0.0.3:8080", weight=150))
    node_after = ring.get_node("session:alice")
    print(f"Alice after scale-out -> {node_after.node_id}")
```

---

## Solution 2: Session-Affinity Router

Use the hash ring to route incoming requests to the correct agent instance, with fallback on unhealthy nodes.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable

class SessionAffinityRouter:
    """
    Routes requests with session/user affinity using consistent hashing.
    On node failure, transparently reroutes to the next healthy node.
    """

    def __init__(self, ring: ConsistentHashRing):
        self._ring = ring
        self._healthy: set[str] = set()
        self._backends: dict[str, Callable] = {}

    def register_backend(self, node_id: str, handler: Callable) -> None:
        self._healthy.add(node_id)
        self._backends[node_id] = handler

    def mark_unhealthy(self, node_id: str) -> None:
        self._healthy.discard(node_id)

    def mark_healthy(self, node_id: str) -> None:
        if node_id in self._backends:
            self._healthy.add(node_id)

    async def route(self, session_key: str, request: Any) -> Any:
        """Route a request to the consistent-hash-assigned node, with failover."""
        candidates = self._ring.get_nodes(session_key, count=len(self._ring.all_nodes()))
        for node in candidates:
            if node.node_id in self._healthy:
                handler = self._backends[node.node_id]
                return await handler(request)
        raise RuntimeError(f"No healthy nodes available for key '{session_key}'")

    def get_affinity(self, session_key: str) -> Optional[str]:
        """Return the preferred node ID for a session key."""
        for node in self._ring.get_nodes(session_key, count=3):
            if node.node_id in self._healthy:
                return node.node_id
        return None
```

---

## Solution 3: Rendezvous (Highest Random Weight) Hashing

An alternative to ring hashing — simpler to implement, provides equally good distribution, and has better load balancing with weighted nodes.

```python
import hashlib
import struct

class RendezvousHashRouter:
    """
    Rendezvous (HRW) hashing: for each key, score every node and pick the highest scorer.
    Advantage: no virtual nodes needed, simpler, uniform distribution.
    On node removal, only keys assigned to that node are remapped.
    """

    def __init__(self):
        self._nodes: dict[str, float] = {}  # node_id -> weight

    def add_node(self, node_id: str, weight: float = 1.0) -> None:
        self._nodes[node_id] = weight

    def remove_node(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)

    def _score(self, key: str, node_id: str, weight: float) -> float:
        """Compute score for (key, node) pair."""
        combined = f"{key}:{node_id}".encode()
        digest = hashlib.sha256(combined).digest()
        # Convert 8 bytes to float in [0, 1)
        raw = struct.unpack(">Q", digest[:8])[0]
        uniform = raw / (2**64)
        # Apply weight: score = -weight / log(uniform) — gives weighted HRW
        import math
        if uniform == 0:
            return float("inf")
        return -weight / math.log(uniform)

    def get_node(self, key: str) -> Optional[str]:
        """Find the node with highest score for this key."""
        if not self._nodes:
            return None
        return max(self._nodes, key=lambda n: self._score(key, n, self._nodes[n]))

    def get_nodes(self, key: str, count: int) -> list[str]:
        """Get top `count` nodes by score — for replication."""
        if not self._nodes:
            return []
        scored = sorted(
            self._nodes,
            key=lambda n: self._score(key, n, self._nodes[n]),
            reverse=True,
        )
        return scored[:count]
```

---

## Solution 4: Work Distribution with Shard Ownership

Assign ownership of data shards to agent instances using the hash ring. On scale events, migrate only the affected shards.

```python
from dataclasses import dataclass, field

@dataclass
class Shard:
    shard_id: int
    owner_node_id: str
    data: dict = field(default_factory=dict)
    is_migrating: bool = False

class ShardedWorkDistributor:
    """
    Distributes work across agent instances using shard ownership.
    Each shard is owned by the consistent-hash-assigned node.
    Scale events trigger shard migration only for affected shards.
    """

    def __init__(self, ring: ConsistentHashRing, num_shards: int = 256):
        self._ring = ring
        self._num_shards = num_shards
        self._shards: dict[int, Shard] = {
            i: Shard(shard_id=i, owner_node_id="") for i in range(num_shards)
        }
        self._reassign_all()

    def _shard_for_key(self, key: str) -> int:
        h = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        return h % self._num_shards

    def _reassign_all(self) -> None:
        for shard_id, shard in self._shards.items():
            node = self._ring.get_node(f"shard:{shard_id}")
            if node:
                shard.owner_node_id = node.node_id

    def add_node(self, node: RingNode) -> list[int]:
        """Add node, return list of shard IDs that migrate to it."""
        self._ring.add_node(node)
        migrating = []
        for shard_id, shard in self._shards.items():
            new_node = self._ring.get_node(f"shard:{shard_id}")
            if new_node and new_node.node_id != shard.owner_node_id:
                shard.is_migrating = True
                migrating.append(shard_id)
                shard.owner_node_id = new_node.node_id
        return migrating

    def remove_node(self, node_id: str) -> list[int]:
        """Remove node, return list of shard IDs that migrate away."""
        self._ring.remove_node(node_id)
        migrating = []
        for shard_id, shard in self._shards.items():
            if shard.owner_node_id == node_id:
                new_node = self._ring.get_node(f"shard:{shard_id}")
                if new_node:
                    shard.is_migrating = True
                    migrating.append(shard_id)
                    shard.owner_node_id = new_node.node_id
        return migrating

    def get_owner(self, key: str) -> str:
        """Get the node ID responsible for this key."""
        shard_id = self._shard_for_key(key)
        return self._shards[shard_id].owner_node_id

    def shards_for_node(self, node_id: str) -> list[int]:
        return [sid for sid, s in self._shards.items() if s.owner_node_id == node_id]

    def migration_plan(self) -> dict[str, list[int]]:
        """Group migrating shards by their new owner."""
        plan: dict[str, list[int]] = {}
        for shard_id, shard in self._shards.items():
            if shard.is_migrating:
                plan.setdefault(shard.owner_node_id, []).append(shard_id)
        return plan
```

---

## Solution 5: Cache-Local Routing

Route requests to the node that has the hottest cache for the requested data by combining consistent hashing with a lightweight cache inventory.

```python
import time
from collections import defaultdict

class CacheLocalRouter:
    """
    Routes requests to the node with the highest probability of cache hit.
    Falls back to consistent-hash node when no cache inventory is available.
    """

    def __init__(self, ring: ConsistentHashRing, ttl: float = 300.0):
        self._ring = ring
        self._cache_inventory: dict[str, dict[str, float]] = defaultdict(dict)
        # cache_inventory[key][node_id] = expiry_time
        self._ttl = ttl

    def report_cache_hit(self, key: str, node_id: str) -> None:
        """A node reports it has this key in its cache."""
        self._cache_inventory[key][node_id] = time.monotonic() + self._ttl

    def evict(self, key: str, node_id: str) -> None:
        """A node reports it evicted this key."""
        self._cache_inventory[key].pop(node_id, None)

    def get_best_node(self, key: str, healthy_nodes: set[str]) -> Optional[str]:
        """Return the node most likely to have a cache hit."""
        now = time.monotonic()

        # Find nodes with live cache entries
        live_holders = [
            nid for nid, exp in self._cache_inventory.get(key, {}).items()
            if exp > now and nid in healthy_nodes
        ]

        if live_holders:
            # Route to a random holder (load balance among hot nodes)
            import random
            return random.choice(live_holders)

        # No cached node — fall back to consistent hash
        node = self._ring.get_node(key)
        if node and node.node_id in healthy_nodes:
            return node.node_id

        # Fallback: any healthy node
        return next(iter(healthy_nodes), None)
```

---

## Solution 6: Cluster Membership Manager

Track which agent instances are currently alive and automatically rebalance the hash ring on membership changes.

```python
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

class ClusterMembershipManager:
    """
    Monitors agent cluster membership and updates the hash ring automatically.
    Uses heartbeat-based failure detection.
    """

    HEARTBEAT_INTERVAL = 5.0
    FAILURE_THRESHOLD  = 15.0  # seconds without heartbeat = node dead

    def __init__(
        self,
        ring: ConsistentHashRing,
        local_node_id: str,
        on_rebalance: Optional[Callable] = None,
    ):
        self._ring = ring
        self._local_id = local_node_id
        self._on_rebalance = on_rebalance
        self._last_heartbeat: dict[str, float] = {}
        self._monitor_task: Optional[asyncio.Task] = None

    def receive_heartbeat(self, node_id: str, node: RingNode) -> None:
        self._last_heartbeat[node_id] = time.monotonic()
        if node_id not in {n.node_id for n in self._ring.all_nodes()}:
            logger.info("New node joined: %s", node_id)
            self._ring.add_node(node)
            if self._on_rebalance:
                asyncio.create_task(self._on_rebalance("join", node_id))

    def start(self) -> None:
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            self._check_for_dead_nodes()

    def _check_for_dead_nodes(self) -> None:
        now = time.monotonic()
        for node in list(self._ring.all_nodes()):
            last_hb = self._last_heartbeat.get(node.node_id, 0)
            if node.node_id != self._local_id and now - last_hb > self.FAILURE_THRESHOLD:
                logger.warning("Node %s appears dead (last hb %.0fs ago)", node.node_id, now - last_hb)
                self._ring.remove_node(node.node_id)
                if self._on_rebalance:
                    asyncio.create_task(self._on_rebalance("leave", node.node_id))

    def get_owner(self, key: str) -> Optional[str]:
        node = self._ring.get_node(key)
        return node.node_id if node else None

    def is_mine(self, key: str) -> bool:
        """Return True if this node is responsible for the given key."""
        return self.get_owner(key) == self._local_id
```

---

## Comparison

| Solution | Remapping on Change | Virtual Nodes | Weighted | Best For |
|---|---|---|---|---|
| Consistent Hash Ring | O(K/N) | Yes | Via weight param | General work distribution |
| Session Affinity Router | O(K/N) | Yes | Via weight | Session-sticky request routing |
| Rendezvous HRW | O(K/N) | No | Yes (native) | Simpler weighted distribution |
| Sharded Work Distributor | O(shards/N) | Yes | No | Data ownership with migrations |
| Cache-Local Router | O(K/N) | Yes | No | Cache-affinity routing |
| Cluster Membership Manager | O(K/N) | Yes | No | Dynamic cluster with failure detection |

**Use the consistent hash ring** as the foundation for any work distribution system. **Add the session affinity router** on top to handle unhealthy node failover. **Use rendezvous hashing** if you need simpler code — it requires no virtual node tuning and is equally consistent. **Add sharded work distribution** for stateful workloads that require explicit data ownership and migration. **Always combine with the cluster membership manager** so topology changes are detected and the ring stays current automatically.
