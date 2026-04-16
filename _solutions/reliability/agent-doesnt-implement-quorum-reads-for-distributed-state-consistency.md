---
title: "Agent Doesn't Implement Quorum Reads for Distributed State Consistency"
description: "AI agents that read shared state from a single replica in a distributed deployment may observe stale data when replicas lag behind the primary. Quorum reads consult multiple replicas and return the result only when a majority agree, preventing the agent from acting on an outdated value that another agent instance has already superseded."
date: 2025-02-17
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-quorum-reads-for-distributed-state-consistency
tags:
  - quorum
  - distributed-state
  - consistency
  - replication
  - reliability
  - consensus
  - stale-reads
symptoms:
  - "Two agent instances read different values for the same key from different replicas"
  - "Agent reads a task as unclaimed and claims it, but another instance claimed it 100ms earlier"
  - "No majority check before acting on shared state in a multi-replica deployment"
  - "Cache replica lag causes agent to see outdated session data after a write"
  - "Read-your-writes consistency violated across agent instances"
---

## Problem

In a multi-instance agent deployment backed by a replicated data store (Redis Cluster, DynamoDB, Postgres streaming replicas), a read from a single replica may return a value that hasn't yet received a write propagated from another replica. Two agent instances reading different replicas can both see a task as available and both claim it, producing duplicate work. Quorum reads solve this by requiring a majority of replicas to return the same value before the read is accepted — if they disagree, the read is retried or rejected, preventing stale-state-driven decisions.

---

## Solution 1: QuorumReader — Majority-Vote Read from Multiple Replicas

```python
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class QuorumResult:
    value: Any
    agreed_count: int
    total_queried: int
    quorum_reached: bool
    conflicting_values: List[Any]

    def majority_fraction(self) -> float:
        if self.total_queried == 0:
            return 0.0
        return self.agreed_count / self.total_queried


class QuorumReader:
    """
    Reads a key from multiple replica clients and returns the value
    only when a quorum (majority) of replicas agree. Conflicting reads
    indicate replication lag and trigger a stale-read warning.

    Usage:
        reader = QuorumReader(
            replicas=[redis_0, redis_1, redis_2],
            read_fn=lambda r, k: r.get(k),
            quorum_size=2,   # 2 of 3 must agree
        )
        result = await reader.read("task:claim:task_001")
        if not result.quorum_reached:
            raise StaleReadError("Replicas disagree — retry after replication")
        value = result.value
    """

    def __init__(self, replicas: List[Any],
                  read_fn: Callable,
                  quorum_size: Optional[int] = None,
                  timeout_s: float = 1.0):
        self._replicas = replicas
        self._read_fn = read_fn
        self._quorum = quorum_size or (len(replicas) // 2 + 1)
        self._timeout = timeout_s

    async def _read_one(self, replica: Any, key: str) -> Optional[Any]:
        try:
            return await asyncio.wait_for(
                self._read_fn(replica, key), timeout=self._timeout
            )
        except Exception as exc:
            logger.warning("quorum_replica_error key=%s error=%s", key, exc)
            return None

    async def read(self, key: str) -> QuorumResult:
        tasks = [self._read_one(r, key) for r in self._replicas]
        raw_results = await asyncio.gather(*tasks)
        responses = [r for r in raw_results if r is not None]

        if not responses:
            return QuorumResult(
                value=None, agreed_count=0,
                total_queried=len(self._replicas),
                quorum_reached=False, conflicting_values=[],
            )

        counts = Counter(
            r if not isinstance(r, (dict, list)) else str(r)
            for r in responses
        )
        top_repr, top_count = counts.most_common(1)[0]
        top_value = next(r for r in responses
                          if (r if not isinstance(r, (dict, list)) else str(r)) == top_repr)

        conflicting = [r for r in responses
                        if (r if not isinstance(r, (dict, list)) else str(r)) != top_repr]

        quorum_reached = top_count >= self._quorum
        if conflicting:
            logger.warning(
                "quorum_conflict key=%s agreed=%d total=%d",
                key, top_count, len(responses),
            )

        return QuorumResult(
            value=top_value,
            agreed_count=top_count,
            total_queried=len(self._replicas),
            quorum_reached=quorum_reached,
            conflicting_values=conflicting,
        )
```

---

## Solution 2: VersionedQuorumStore — Resolve Conflicts by Version Number

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class VersionedValue:
    value: Any
    version: int
    written_at: float


class VersionedQuorumStore:
    """
    Extends quorum reads with version numbers: each write increments a
    version counter, and quorum reads return the value with the highest
    version among the majority. Resolves conflicts caused by network
    partitions by always preferring the newest version.

    Usage:
        store = VersionedQuorumStore(replicas, write_fn, read_fn)
        versioned = await store.quorum_read("session:u123")
        if versioned.version < expected_min_version:
            raise ConsistencyError("Read is too stale")
    """

    def __init__(self, replicas: List[Any],
                  write_fn: Callable,
                  read_fn: Callable,
                  quorum_size: Optional[int] = None,
                  timeout_s: float = 1.0):
        self._replicas = replicas
        self._write = write_fn
        self._read = read_fn
        self._quorum = quorum_size or (len(replicas) // 2 + 1)
        self._timeout = timeout_s

    async def _read_versioned(self, replica: Any,
                               key: str) -> Optional[VersionedValue]:
        try:
            result = await asyncio.wait_for(
                self._read(replica, key), timeout=self._timeout
            )
            if result is None:
                return None
            if isinstance(result, dict) and "_version" in result:
                return VersionedValue(
                    value=result.get("value"),
                    version=int(result["_version"]),
                    written_at=float(result.get("_written_at", 0)),
                )
            return VersionedValue(value=result, version=0, written_at=0)
        except Exception as exc:
            logger.warning("versioned_read_error key=%s error=%s", key, exc)
            return None

    async def quorum_read(self, key: str) -> Optional[VersionedValue]:
        tasks = [self._read_versioned(r, key) for r in self._replicas]
        results = [r for r in await asyncio.gather(*tasks) if r is not None]

        if len(results) < self._quorum:
            logger.warning(
                "quorum_unavailable key=%s responses=%d required=%d",
                key, len(results), self._quorum,
            )
            return None

        # Return highest version seen by at least quorum replicas
        results.sort(key=lambda v: -v.version)
        highest_version = results[0].version
        count_at_highest = sum(1 for r in results if r.version == highest_version)

        if count_at_highest >= self._quorum:
            return results[0]

        # No single version has quorum — use newest as best-effort
        logger.warning(
            "quorum_version_split key=%s max_version=%d agreed=%d",
            key, highest_version, count_at_highest,
        )
        return results[0]

    async def quorum_write(self, key: str, value: Any,
                             current_version: int) -> bool:
        """Write to all replicas with version increment."""
        new_version = current_version + 1
        payload = {
            "value": value,
            "_version": new_version,
            "_written_at": time.time(),
        }
        tasks = [
            asyncio.wait_for(self._write(r, key, payload), timeout=self._timeout)
            for r in self._replicas
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successes = sum(1 for r in results if not isinstance(r, Exception))
        return successes >= self._quorum
```

---

## Solution 3: FencedClaimManager — Claim Tasks with Quorum + Fencing Token

```python
import asyncio
import logging
import time
import uuid
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class FencedClaimManager:
    """
    Implements distributed task claiming using quorum writes and fencing
    tokens. A fencing token (monotonically increasing) is included in the
    claim so that stale claims from slow agents are rejected by the task
    processor (which accepts only the highest token it has seen).

    Usage:
        manager = FencedClaimManager(replicas, read_fn, cas_fn)
        token = await manager.claim("task:001", agent_id="agent-A", ttl_s=30)
        if token is None:
            # Task already claimed by another agent
            return
        # Token proves claim is valid — include in all operations on this task
    """

    def __init__(self, replicas: List[Any],
                  read_fn: Callable,
                  cas_fn: Callable,   # compare-and-swap(replica, key, expected, new)
                  quorum_size: Optional[int] = None,
                  timeout_s: float = 1.0):
        self._replicas = replicas
        self._read = read_fn
        self._cas = cas_fn
        self._quorum = quorum_size or (len(replicas) // 2 + 1)
        self._timeout = timeout_s
        self._reader = QuorumReader(replicas, read_fn, quorum_size, timeout_s)

    async def claim(self, task_key: str,
                     agent_id: str,
                     ttl_s: float = 30.0) -> Optional[int]:
        """
        Attempts to claim task_key for agent_id.
        Returns a fencing token (int) on success, None if already claimed.
        """
        result = await self._reader.read(task_key)

        if result.value is not None:
            logger.debug("task_already_claimed key=%s", task_key)
            return None  # Already claimed

        fencing_token = int(time.time() * 1000)  # Monotonic ms timestamp
        claim_payload = {
            "agent_id": agent_id,
            "token": fencing_token,
            "claimed_at": time.time(),
            "expires_at": time.time() + ttl_s,
        }

        # CAS: set only if still None
        tasks = [
            asyncio.wait_for(
                self._cas(r, task_key, None, claim_payload),
                timeout=self._timeout,
            )
            for r in self._replicas
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successes = sum(1 for r in results if r is True)

        if successes >= self._quorum:
            logger.info(
                "task_claimed key=%s agent=%s token=%d successes=%d",
                task_key, agent_id, fencing_token, successes,
            )
            return fencing_token

        # Roll back partial claims
        for i, r in enumerate(results):
            if r is True:
                try:
                    await self._replicas[i].delete(task_key)
                except Exception:
                    pass
        return None
```

---

## Solution 4: ReplicaLagMonitor — Detect and Exclude Lagging Replicas

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReplicaHealth:
    replica_id: str
    lag_s: float
    last_checked: float
    healthy: bool


class ReplicaLagMonitor:
    """
    Periodically measures replication lag for each replica by comparing
    a write-timestamp sentinel value. Excludes replicas whose lag exceeds
    a threshold from quorum reads to prevent consistently stale replicas
    from polluting quorum decisions.

    Usage:
        monitor = ReplicaLagMonitor(replicas, write_fn, read_fn, max_lag_s=0.5)
        await monitor.check_all()
        healthy = monitor.healthy_replicas()
        reader = QuorumReader(healthy, read_fn)
    """

    SENTINEL_KEY = "__replica_lag_probe__"

    def __init__(self, replicas: List[Any],
                  write_fn: Callable,
                  read_fn: Callable,
                  max_lag_s: float = 0.5,
                  timeout_s: float = 1.0):
        self._replicas = replicas
        self._write = write_fn
        self._read = read_fn
        self._max_lag = max_lag_s
        self._timeout = timeout_s
        self._health: Dict[int, ReplicaHealth] = {}

    async def check_all(self):
        now = time.time()
        # Write sentinel to primary (replica 0)
        try:
            await asyncio.wait_for(
                self._write(self._replicas[0], self.SENTINEL_KEY, str(now)),
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.error("sentinel_write_failed error=%s", exc)
            return

        await asyncio.sleep(0.05)  # Brief propagation window

        tasks = [
            self._check_replica(i, r, now)
            for i, r in enumerate(self._replicas)
        ]
        await asyncio.gather(*tasks)

    async def _check_replica(self, idx: int, replica: Any, written_at: float):
        try:
            raw = await asyncio.wait_for(
                self._read(replica, self.SENTINEL_KEY),
                timeout=self._timeout,
            )
            replica_ts = float(raw) if raw else 0.0
            lag = time.time() - replica_ts
            healthy = lag <= self._max_lag
        except Exception:
            lag = float("inf")
            healthy = False

        self._health[idx] = ReplicaHealth(
            replica_id=f"replica_{idx}",
            lag_s=lag,
            last_checked=time.time(),
            healthy=healthy,
        )
        if not healthy:
            logger.warning(
                "replica_lagging id=replica_%d lag_s=%.2f", idx, lag
            )

    def healthy_replicas(self) -> List[Any]:
        return [
            r for i, r in enumerate(self._replicas)
            if self._health.get(i, ReplicaHealth("", 0, 0, True)).healthy
        ]

    def health_report(self) -> List[Dict[str, Any]]:
        return [
            {"id": h.replica_id, "lag_s": h.lag_s, "healthy": h.healthy}
            for h in self._health.values()
        ]
```

---

## Solution 5: ReadRepairCoordinator — Fix Diverged Replicas on Read

```python
import asyncio
import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class ReadRepairCoordinator:
    """
    Implements read-repair: when a quorum read detects that some replicas
    returned stale values, the coordinator writes the correct (majority)
    value back to the lagging replicas. This gradually converges replicas
    without a background repair job.

    Usage:
        repair = ReadRepairCoordinator(replicas, write_fn)
        result = await quorum_reader.read(key)
        if result.conflicting_values:
            await repair.repair(key, result)
    """

    def __init__(self, replicas: List[Any],
                  write_fn: Callable,
                  timeout_s: float = 1.0):
        self._replicas = replicas
        self._write = write_fn
        self._timeout = timeout_s
        self._repair_count = 0

    async def repair(self, key: str, result: QuorumResult):
        """Write the majority value to any replicas that returned a stale value."""
        if not result.quorum_reached or not result.conflicting_values:
            return

        correct_value = result.value
        repair_tasks = []

        for replica in self._replicas:
            repair_tasks.append(
                self._repair_one(replica, key, correct_value)
            )

        await asyncio.gather(*repair_tasks)
        self._repair_count += 1
        logger.info(
            "read_repair_applied key=%s conflicting=%d",
            key, len(result.conflicting_values),
        )

    async def _repair_one(self, replica: Any, key: str, value: Any):
        try:
            await asyncio.wait_for(
                self._write(replica, key, value),
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.warning("read_repair_error key=%s error=%s", key, exc)

    def total_repairs(self) -> int:
        return self._repair_count
```

---

## Solution 6: QuorumConsistencyLayer — Full Stack Integration

```python
import asyncio
import logging
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


class QuorumConsistencyLayer:
    """
    Integrates quorum reads, versioned writes, replica lag monitoring,
    and read-repair into a unified consistency layer for shared agent state.

    Usage:
        layer = QuorumConsistencyLayer(
            replicas=[r0, r1, r2],
            read_fn=lambda r, k: r.get(k),
            write_fn=lambda r, k, v: r.set(k, v),
            cas_fn=lambda r, k, exp, new: r.cas(k, exp, new),
        )
        await layer.check_health()

        value = await layer.consistent_read("agent:state:session_1")
        await layer.consistent_write("agent:state:session_1", new_state)
    """

    def __init__(self, replicas: List[Any],
                  read_fn: Callable,
                  write_fn: Callable,
                  cas_fn: Optional[Callable] = None,
                  max_lag_s: float = 0.5,
                  timeout_s: float = 1.0):
        self._replicas = replicas
        self._lag_monitor = ReplicaLagMonitor(
            replicas, write_fn, read_fn, max_lag_s, timeout_s
        )
        self._repair = ReadRepairCoordinator(replicas, write_fn, timeout_s)
        self._read_fn = read_fn
        self._write_fn = write_fn
        self._timeout = timeout_s

    async def check_health(self):
        await self._lag_monitor.check_all()

    def _make_reader(self) -> QuorumReader:
        healthy = self._lag_monitor.healthy_replicas()
        if not healthy:
            logger.warning("no_healthy_replicas falling_back_to_all")
            healthy = self._replicas
        return QuorumReader(healthy, self._read_fn, timeout_s=self._timeout)

    async def consistent_read(self, key: str) -> Optional[Any]:
        reader = self._make_reader()
        result = await reader.read(key)
        if result.conflicting_values:
            await self._repair.repair(key, result)
        if not result.quorum_reached:
            raise RuntimeError(
                f"Quorum not reached for key '{key}' "
                f"({result.agreed_count}/{result.total_queried})"
            )
        return result.value

    async def consistent_write(self, key: str, value: Any) -> bool:
        tasks = [
            asyncio.wait_for(
                self._write_fn(r, key, value), timeout=self._timeout
            )
            for r in self._replicas
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        quorum = len(self._replicas) // 2 + 1
        successes = sum(1 for r in results if not isinstance(r, Exception))
        return successes >= quorum

    def health_report(self) -> dict:
        return {
            "replicas": self._lag_monitor.health_report(),
            "total_repairs": self._repair.total_repairs(),
        }
```

---

## Comparison

| Approach | Majority Vote | Version Tracking | Fencing Tokens | Lag Monitoring | Read Repair | Integrated |
|---|---|---|---|---|---|---|
| **QuorumReader** | Yes | No | No | No | No | No |
| **VersionedQuorumStore** | Yes | Yes | No | No | No | No |
| **FencedClaimManager** | Yes | No | Yes | No | No | No |
| **ReplicaLagMonitor** | No | No | No | Yes | No | No |
| **ReadRepairCoordinator** | No | No | No | No | Yes | No |
| **QuorumConsistencyLayer** | Yes | No | No | Yes | Yes | Yes |

**Key insight**: quorum size = `floor(N/2) + 1` for N replicas guarantees overlap — any two quorums share at least one member, so a write seen by quorum W is always visible to a subsequent read quorum R. The biggest practical issue is not algorithmic but operational: replica lag spikes during network events can make quorum reads slow (waiting for slow replicas). `ReplicaLagMonitor` excludes persistently lagging replicas before they affect read latency, at the cost of a smaller effective quorum. Always set `timeout_s` per-replica to avoid the slowest replica blocking the entire operation.
