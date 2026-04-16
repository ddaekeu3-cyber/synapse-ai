---
title: "Agent Doesn't Implement Hierarchical Lock Ordering to Prevent Deadlock"
description: "AI agents managing multiple shared resources — session state, tool registries, credential caches — can deadlock when two coroutines acquire the same locks in different orders. Hierarchical lock ordering assigns a total order to all locks; acquiring them lowest-to-highest makes deadlock mathematically impossible."
date: 2025-02-04
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-hierarchical-lock-ordering-to-prevent-deadlock
tags:
  - deadlock
  - lock-ordering
  - mutex
  - concurrency
  - asyncio
  - liveness
  - resource-allocation
symptoms:
  - "Agent hangs indefinitely under concurrent load with no error message"
  - "CPU drops to zero and all requests queue up — classic deadlock signature"
  - "Two agent workers are stuck waiting for each other's locks"
  - "Deadlock only reproduces under high concurrency and is impossible to reproduce in unit tests"
  - "Adding a new shared resource breaks previously stable concurrent code"
---

## Problem

Deadlock occurs when coroutine A holds lock X and waits for lock Y, while coroutine B holds lock Y and waits for lock X. Neither can proceed. In async Python this manifests as an event loop that stops processing requests while all tasks sit in `await lock.acquire()`.

The classic prevention is **lock ordering**: assign each lock a unique integer level; always acquire lower-level locks before higher-level ones. If every piece of code obeys this ordering, a cycle in the wait-for graph becomes impossible.

---

## Solution 1: Numbered Lock with Acquisition Order Enforcement

Wrap `asyncio.Lock` with an integer level. A context manager enforces that you cannot acquire a level-N lock while holding a level-M lock where M > N.

```python
import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Set

# Per-task set of currently held lock levels
_held_levels: contextvars.ContextVar[Set[int]] = contextvars.ContextVar(
    "_held_levels", default=None
)


class OrderedLock:
    """
    Asyncio lock with a mandatory acquisition level.
    Raises if you try to acquire this lock while holding a higher-level lock.

    Usage:
        session_lock  = OrderedLock(level=1, name="session")
        tool_lock     = OrderedLock(level=2, name="tool_registry")
        cred_lock     = OrderedLock(level=3, name="credentials")

        # CORRECT: acquire lowest level first
        async with session_lock:
            async with tool_lock:
                async with cred_lock:
                    ...

        # WRONG (raises LockOrderViolation):
        async with cred_lock:
            async with session_lock:   # ← would raise
                ...
    """

    def __init__(self, level: int, name: str = ""):
        self.level = level
        self.name = name or f"lock-{level}"
        self._lock = asyncio.Lock()

    async def acquire(self):
        held = _held_levels.get(None)
        if held is None:
            held = set()
            _held_levels.set(held)
        if any(h > self.level for h in held):
            violators = {h for h in held if h > self.level}
            raise LockOrderViolation(
                f"Attempted to acquire {self.name} (level={self.level}) "
                f"while holding locks at higher levels: {violators}. "
                f"Acquire locks in ascending level order to prevent deadlock."
            )
        await self._lock.acquire()
        held.add(self.level)

    def release(self):
        held = _held_levels.get(None)
        if held is not None:
            held.discard(self.level)
        self._lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        self.release()

    def locked(self) -> bool:
        return self._lock.locked()


class LockOrderViolation(RuntimeError):
    pass
```

---

## Solution 2: Lock Hierarchy Registry

Centrally register all locks with their levels. The registry validates the acquisition order and provides a global view of which locks exist at which levels.

```python
import asyncio
from typing import Dict, List, Optional


class LockHierarchyRegistry:
    """
    Central registry for all named locks in the agent.
    Ensures no two locks share the same level and provides factory methods.

    Usage:
        registry = LockHierarchyRegistry()
        session_lock  = registry.create(level=10, name="session")
        memory_lock   = registry.create(level=20, name="memory")
        tool_lock     = registry.create(level=30, name="tool_registry")

        # Acquire in order:
        async with registry.multi_acquire("session", "memory"):
            ...  # locks acquired in level order automatically
    """

    def __init__(self):
        self._locks: Dict[str, OrderedLock] = {}
        self._levels: Dict[int, str] = {}

    def create(self, level: int, name: str) -> OrderedLock:
        if level in self._levels:
            existing = self._levels[level]
            raise ValueError(
                f"Level {level} already assigned to '{existing}'. "
                f"Choose a unique level for '{name}'."
            )
        if name in self._locks:
            raise ValueError(f"Lock '{name}' already registered.")
        lock = OrderedLock(level=level, name=name)
        self._locks[name] = lock
        self._levels[level] = name
        return lock

    def get(self, name: str) -> OrderedLock:
        lock = self._locks.get(name)
        if lock is None:
            raise KeyError(f"Lock '{name}' not registered.")
        return lock

    def multi_acquire(self, *names: str) -> "_MultiLockContext":
        """Acquire multiple locks in ascending level order."""
        locks = [self.get(n) for n in names]
        locks.sort(key=lambda l: l.level)
        return _MultiLockContext(locks)

    def hierarchy_diagram(self) -> str:
        lines = ["Lock Hierarchy:"]
        for level in sorted(self._levels):
            name = self._levels[level]
            locked = self._locks[name].locked()
            status = "LOCKED" if locked else "free"
            lines.append(f"  Level {level:4d}: {name} [{status}]")
        return "\n".join(lines)


class _MultiLockContext:
    def __init__(self, locks: List[OrderedLock]):
        self._locks = locks
        self._acquired: List[OrderedLock] = []

    async def __aenter__(self):
        for lock in self._locks:
            await lock.acquire()
            self._acquired.append(lock)
        return self

    async def __aexit__(self, *_):
        for lock in reversed(self._acquired):
            lock.release()
        self._acquired.clear()
```

---

## Solution 3: Deadlock Detector (Cycle Detection in Wait-For Graph)

A background monitor builds a wait-for graph and checks for cycles. When a cycle is detected, it logs the deadlock participants and optionally raises a timeout exception on the longest-waiting coroutine.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class WaitEdge:
    waiter_task: str      # task name
    held_by_task: str     # the task holding the lock the waiter needs
    lock_name: str
    wait_since: float = field(default_factory=time.monotonic)


class DeadlockDetector:
    """
    Background task that builds and checks the wait-for graph.
    Raises DeadlockError when a cycle is detected.

    Usage:
        detector = DeadlockDetector(check_interval=1.0)
        asyncio.create_task(detector.run())

        # Instrument your locks:
        detector.record_wait("task-A", held_by="task-B", lock="session_lock")
        detector.record_acquire("task-A", lock="session_lock")
        detector.record_release("task-B", lock="session_lock")
    """

    def __init__(self, check_interval: float = 1.0):
        self._interval = check_interval
        self._edges: Dict[str, WaitEdge] = {}   # waiter -> edge
        self._lock_holders: Dict[str, str] = {} # lock_name -> task_name

    def record_wait(self, waiter: str, held_by: str, lock: str):
        self._edges[waiter] = WaitEdge(waiter, held_by, lock)

    def record_acquire(self, task: str, lock: str):
        self._lock_holders[lock] = task
        self._edges.pop(task, None)

    def record_release(self, task: str, lock: str):
        if self._lock_holders.get(lock) == task:
            del self._lock_holders[lock]

    def find_cycle(self) -> Optional[List[str]]:
        """Return the cycle path if one exists, else None."""
        def dfs(node: str, path: List[str], visited: Set[str]) -> Optional[List[str]]:
            if node in path:
                cycle_start = path.index(node)
                return path[cycle_start:] + [node]
            if node in visited:
                return None
            visited.add(node)
            edge = self._edges.get(node)
            if edge is None:
                return None
            return dfs(edge.held_by_task, path + [node], visited)

        for task in list(self._edges.keys()):
            cycle = dfs(task, [], set())
            if cycle:
                return cycle
        return None

    async def run(self):
        while True:
            await asyncio.sleep(self._interval)
            cycle = self.find_cycle()
            if cycle:
                import logging
                logging.getLogger(__name__).error(
                    "DEADLOCK DETECTED: %s", " -> ".join(cycle)
                )

    def wait_times(self) -> Dict[str, float]:
        now = time.monotonic()
        return {
            task: round(now - edge.wait_since, 2)
            for task, edge in self._edges.items()
        }
```

---

## Solution 4: Lock-Free Resource Manager Using Immutable State

Replace mutable shared state + locks with an immutable state + compare-and-swap update model. If two coroutines try to update simultaneously, one wins and the other retries — no locks, no deadlock.

```python
import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class VersionedState(Generic[T]):
    value: T
    version: int = 0


class LockFreeResourceManager(Generic[T]):
    """
    CAS-based lock-free resource manager.
    Updates use optimistic concurrency: read, modify, compare-and-swap.
    No locks → no lock-ordering issues → no deadlock.

    Usage:
        mgr = LockFreeResourceManager(initial_state={"sessions": {}})

        async def add_session(state):
            new = dict(state)
            new["sessions"][session_id] = session_data
            return new

        await mgr.update(add_session)
        current = mgr.read()
    """

    def __init__(self, initial_state: T, max_retries: int = 100):
        self._state = VersionedState(value=initial_state)
        self._max_retries = max_retries
        self._lock = asyncio.Lock()   # Only used for the CAS swap itself

    def read(self) -> T:
        return self._state.value

    async def update(self, fn: Callable[[T], T]) -> T:
        """Apply fn to current state. Retries on concurrent modification."""
        for attempt in range(self._max_retries):
            current = self._state
            new_value = fn(current.value)
            async with self._lock:
                if self._state.version == current.version:
                    self._state = VersionedState(
                        value=new_value,
                        version=current.version + 1,
                    )
                    return new_value
            # Version changed; another coroutine updated first — retry
            if attempt < self._max_retries - 1:
                await asyncio.sleep(0)  # yield
        raise RuntimeError(
            f"CAS failed after {self._max_retries} retries — high contention"
        )

    @property
    def version(self) -> int:
        return self._state.version
```

---

## Solution 5: Timed Lock with Deadlock Timeout

Wrap `asyncio.Lock` so that acquisition has a maximum wait time. If a coroutine cannot acquire the lock within the timeout, it raises `DeadlockSuspected` instead of hanging forever.

```python
import asyncio
import time
from typing import Optional


class TimedLock:
    """
    Lock with a configurable acquisition timeout.
    Raises DeadlockSuspected if the lock is not acquired in time.

    Usage:
        lock = TimedLock(timeout=5.0, name="session_state")
        async with lock:
            await mutate_session()
        # Raises DeadlockSuspected if waited > 5.0 s
    """

    def __init__(self, timeout: float = 5.0, name: str = ""):
        self._timeout = timeout
        self.name = name
        self._lock = asyncio.Lock()
        self._waiters = 0

    async def acquire(self):
        self._waiters += 1
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise DeadlockSuspected(
                f"Lock '{self.name}' not acquired within {self._timeout}s. "
                f"Current waiters: {self._waiters}. Possible deadlock."
            )
        finally:
            self._waiters -= 1

    def release(self):
        self._lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        self.release()

    @property
    def n_waiting(self) -> int:
        return self._waiters


class DeadlockSuspected(RuntimeError):
    pass
```

---

## Solution 6: Automatic Lock Ordering Agent Mixin

A mixin that an agent class inherits to get automatic lock-ordering enforcement on all registered locks, plus a deadlock detector and timed locks.

```python
import asyncio
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SafeLockingMixin:
    """
    Mixin for agents that need multiple locks.
    All locks are registered with levels; acquisition is always level-ordered.

    Usage:
        class MyAgent(SafeLockingMixin):
            def __init__(self):
                super().__init__()
                self._session_lock = self.register_lock(level=10, name="session")
                self._tool_lock    = self.register_lock(level=20, name="tool_registry")
                self._cred_lock    = self.register_lock(level=30, name="credentials")

            async def update_session_and_tool(self):
                # Acquires in level order: 10 then 20
                async with self.multi_lock("session", "tool_registry"):
                    ...
    """

    def __init__(self):
        self._lock_registry = LockHierarchyRegistry()
        self._detector = DeadlockDetector(check_interval=2.0)
        self._timed_defaults: Dict[str, float] = {}
        asyncio.create_task(self._detector.run(), name="deadlock_detector")

    def register_lock(self, level: int, name: str,
                       timeout: float = 10.0) -> OrderedLock:
        lock = self._lock_registry.create(level=level, name=name)
        self._timed_defaults[name] = timeout
        return lock

    def multi_lock(self, *names: str) -> "_MultiLockContext":
        return self._lock_registry.multi_acquire(*names)

    def lock_diagram(self) -> str:
        return self._lock_registry.hierarchy_diagram()

    async def safe_update(self, resource_name: str,
                           update_fn, *names: str) -> Any:
        """Acquire `names` in level order, apply update_fn, release."""
        async with self._lock_registry.multi_acquire(*names):
            return await update_fn()
```

---

## Comparison

| Approach | Prevents Deadlock | Detection | Runtime Overhead |
|---|---|---|---|
| **OrderedLock (level check)** | Yes (raises on violation) | At acquire time | Minimal |
| **Lock Hierarchy Registry** | Yes (sorted acquire) | At acquire time | Minimal |
| **Deadlock Detector** | No (detects, alerts) | Background cycle check | Low |
| **Lock-Free CAS Manager** | Yes (no locks) | N/A | CAS retries |
| **Timed Lock** | Partially (timeout escape) | At timeout | Minimal |
| **Safe Locking Mixin** | Yes (registry + detector) | Combined | Low |

**Key insight**: lock-ordering violations are programming errors, not runtime states — enforce them in development with the `OrderedLock` assertion so they never reach production. Use `TimedLock` in production as a safety net for any violation that slips through.
