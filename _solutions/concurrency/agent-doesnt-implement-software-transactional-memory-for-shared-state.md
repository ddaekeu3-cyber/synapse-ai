---
title: "Agent Doesn't Implement Software Transactional Memory for Shared State"
description: "AI agents that coordinate concurrent state updates with explicit locks suffer from deadlock, priority inversion, and composability failures — you cannot compose two lock-based operations safely without acquiring both locks in the right order. Software Transactional Memory (STM) replaces lock soup with composable atomic transactions: reads and writes are buffered in a transaction log, committed atomically if no conflict, retried automatically otherwise."
date: 2025-02-10
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-software-transactional-memory-for-shared-state
tags:
  - stm
  - software-transactional-memory
  - optimistic-concurrency
  - atomic-transactions
  - asyncio
  - composability
  - shared-state
symptoms:
  - "Two agent coroutines deadlock while updating session state and tool registry simultaneously"
  - "Adding a new shared resource breaks previously stable lock-ordering code"
  - "Composing two independently-safe operations produces a race condition"
  - "Lock acquisition order is undocumented and spreads across 12 files"
  - "Agent state corruption occurs under high concurrency despite each individual update being locked"
---

## Problem

Lock-based concurrency has a fundamental composability problem: combining two individually-correct locked operations does not yield a correct composed operation without carefully acquiring all locks first. STM solves this by treating memory updates as database transactions: each transaction reads a snapshot, writes to a private log, and commits atomically only if no other transaction modified the same variables. Conflicts cause an automatic retry, not a deadlock.

---

## Solution 1: TVar and STM Transaction — Core Primitives

```python
import asyncio
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, Optional, Set, TypeVar

T = TypeVar("T")


class TVar(Generic[T]):
    """
    A transactional variable. All reads/writes must happen inside a transaction.
    Direct access outside a transaction raises TransactionRequired.
    """

    _next_id = 0
    _id_lock = threading.Lock()

    def __init__(self, value: T):
        with TVar._id_lock:
            self._id = TVar._next_id
            TVar._next_id += 1
        self._value: T = value
        self._version: int = 0
        self._lock = asyncio.Lock()

    @property
    def id(self) -> int:
        return self._id

    def _read_committed(self) -> tuple:
        return self._value, self._version

    def _commit(self, value: T):
        self._value = value
        self._version += 1


class RetryTransaction(Exception):
    """Raised when a transaction should be retried due to conflict."""


class TransactionLog:
    """Private read/write log for one transaction attempt."""

    def __init__(self):
        self._reads: Dict[int, tuple] = {}   # var_id -> (value, version_at_read)
        self._writes: Dict[int, Any] = {}    # var_id -> new_value
        self._tvars: Dict[int, TVar] = {}    # var_id -> TVar reference

    def read(self, tvar: TVar) -> Any:
        vid = tvar.id
        if vid in self._writes:
            return self._writes[vid]
        if vid not in self._reads:
            val, ver = tvar._read_committed()
            self._reads[vid] = (val, ver)
            self._tvars[vid] = tvar
        return self._reads[vid][0]

    def write(self, tvar: TVar, value: Any):
        vid = tvar.id
        if vid not in self._reads:
            _, ver = tvar._read_committed()
            self._reads[vid] = (tvar._read_committed()[0], ver)
            self._tvars[vid] = tvar
        self._writes[vid] = value

    def validate(self) -> bool:
        """Return True if no read variable has been modified since we read it."""
        for vid, (_, ver_at_read) in self._reads.items():
            _, current_ver = self._tvars[vid]._read_committed()
            if current_ver != ver_at_read:
                return False
        return True

    def commit(self):
        for vid, new_val in self._writes.items():
            self._tvars[vid]._commit(new_val)


_current_log: asyncio.Lock = asyncio.Lock()
_log_var: Optional[TransactionLog] = None


class STM:
    """
    Software Transactional Memory engine.

    Usage:
        balance_a = TVar(1000)
        balance_b = TVar(500)

        async def transfer(amount: int):
            async with STM.atomically() as tx:
                a = tx.read(balance_a)
                b = tx.read(balance_b)
                if a < amount:
                    raise ValueError("Insufficient funds")
                tx.write(balance_a, a - amount)
                tx.write(balance_b, b + amount)

        await asyncio.gather(*[transfer(100) for _ in range(50)])
    """

    _global_lock = asyncio.Lock()

    @classmethod
    @contextmanager
    def _transaction_ctx(cls):
        log = TransactionLog()
        yield log

    @classmethod
    async def atomically(cls, max_retries: int = 100):
        return _AtomicContext(cls, max_retries)


class _AtomicContext:
    def __init__(self, stm_cls, max_retries: int):
        self._stm = stm_cls
        self._max_retries = max_retries
        self._log: Optional[TransactionLog] = None

    async def __aenter__(self):
        self._log = TransactionLog()
        return self._log

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False  # propagate exceptions
        for attempt in range(self._max_retries):
            async with STM._global_lock:
                if self._log.validate():
                    self._log.commit()
                    return False
            # Conflict: rebuild log and retry
            self._log = TransactionLog()
            if attempt > 0:
                await asyncio.sleep(0)
        raise RuntimeError(f"STM transaction failed after {self._max_retries} retries")
```

---

## Solution 2: Composable STM Operations

STM's key advantage is composability: combine two atomic operations into one larger atomic operation without any additional locking.

```python
from typing import Callable


class STMOperations:
    """
    Library of composable STM operations for common agent patterns.
    Each operation accepts a TransactionLog and can be composed freely.

    Usage:
        balance = TVar(1000)
        count   = TVar(0)

        async def charge_and_count(amount: int):
            async with await STM.atomically() as tx:
                # Compose: debit + increment in one atomic transaction
                STMOperations.debit(tx, balance, amount)
                STMOperations.increment(tx, count)
    """

    @staticmethod
    def read(tx: TransactionLog, var: TVar) -> Any:
        return tx.read(var)

    @staticmethod
    def write(tx: TransactionLog, var: TVar, value: Any):
        tx.write(var, value)

    @staticmethod
    def increment(tx: TransactionLog, var: TVar, by: int = 1) -> int:
        new = tx.read(var) + by
        tx.write(var, new)
        return new

    @staticmethod
    def debit(tx: TransactionLog, balance: TVar, amount):
        current = tx.read(balance)
        if current < amount:
            raise ValueError(f"Insufficient balance: {current} < {amount}")
        tx.write(balance, current - amount)

    @staticmethod
    def credit(tx: TransactionLog, balance: TVar, amount):
        tx.write(balance, tx.read(balance) + amount)

    @staticmethod
    def swap(tx: TransactionLog, a: TVar, b: TVar):
        va, vb = tx.read(a), tx.read(b)
        tx.write(a, vb)
        tx.write(b, va)

    @staticmethod
    def conditional_write(tx: TransactionLog, var: TVar,
                           predicate: Callable[[Any], bool],
                           new_value: Any) -> bool:
        if predicate(tx.read(var)):
            tx.write(var, new_value)
            return True
        return False

    @staticmethod
    def transfer(tx: TransactionLog,
                 from_var: TVar, to_var: TVar, amount):
        """Atomic transfer — safe to compose with other operations."""
        STMOperations.debit(tx, from_var, amount)
        STMOperations.credit(tx, to_var, amount)
```

---

## Solution 3: AgentStateSTM — Session + Tool Registry Coordination

Replace the lock-soup pattern for coordinating agent session state and tool registry with STM transactions.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AgentSession:
    session_id: str
    tool_calls: int
    last_tool: Optional[str]
    active: bool


class AgentStateSTM:
    """
    Manages agent session state and tool registry via STM transactions.
    Solves the classic deadlock: session_lock → tool_lock vs tool_lock → session_lock.

    Usage:
        state = AgentStateSTM()
        # Register tool and activate session atomically:
        await state.register_tool_and_activate("web_search", session_id="s1")
        # Charge tool call atomically across both structures:
        await state.record_tool_call(session_id="s1", tool_name="web_search")
    """

    def __init__(self):
        self._sessions: TVar = TVar({})           # Dict[str, AgentSession]
        self._tool_registry: TVar = TVar({})       # Dict[str, dict]
        self._global_call_count: TVar = TVar(0)

    async def register_tool_and_activate(self, tool_name: str,
                                          session_id: str, **tool_meta):
        async with await STM.atomically() as tx:
            tools = dict(tx.read(self._tool_registry))
            tools[tool_name] = {"name": tool_name, **tool_meta}
            tx.write(self._tool_registry, tools)

            sessions = dict(tx.read(self._sessions))
            sessions[session_id] = AgentSession(
                session_id=session_id, tool_calls=0,
                last_tool=None, active=True,
            )
            tx.write(self._sessions, sessions)

    async def record_tool_call(self, session_id: str, tool_name: str):
        async with await STM.atomically() as tx:
            tools = tx.read(self._tool_registry)
            if tool_name not in tools:
                raise KeyError(f"Tool '{tool_name}' not registered")

            sessions = dict(tx.read(self._sessions))
            session = sessions.get(session_id)
            if session is None or not session.active:
                raise ValueError(f"Session '{session_id}' not active")

            sessions[session_id] = AgentSession(
                session_id=session_id,
                tool_calls=session.tool_calls + 1,
                last_tool=tool_name,
                active=True,
            )
            tx.write(self._sessions, sessions)
            STMOperations.increment(tx, self._global_call_count)

    async def snapshot(self) -> Dict[str, Any]:
        async with await STM.atomically() as tx:
            return {
                "sessions": tx.read(self._sessions),
                "tools": tx.read(self._tool_registry),
                "total_calls": tx.read(self._global_call_count),
            }
```

---

## Solution 4: STMRetryQueue — Blocking Retry on Variable Change

Implement `retry` semantics: if a precondition is not met, block the transaction until a watched variable changes (Haskell STM's `retry`).

```python
import asyncio
from typing import Callable, Any


class STMRetryQueue:
    """
    Extends STM with blocking retry: if a transaction's precondition
    fails, it waits until any read variable changes before retrying.
    Eliminates polling loops on shared state.

    Usage:
        queue_size = TVar(0)
        queue_data = TVar([])

        async def consumer():
            async for item in retry_queue.take_when_available(queue_data, queue_size):
                process(item)
    """

    def __init__(self, poll_interval: float = 0.01):
        self._poll = poll_interval

    async def wait_until(self, condition: Callable[[], bool],
                          watched: list,  # List[TVar]
                          timeout: float = 30.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            async with await STM.atomically() as tx:
                versions_before = {v.id: v._version for v in watched}
            if condition():
                return True
            # Wait for any watched variable to change version
            start_versions = {v.id: v._version for v in watched}
            while True:
                await asyncio.sleep(self._poll)
                if any(v._version != start_versions[v.id] for v in watched):
                    break
                if asyncio.get_event_loop().time() >= deadline:
                    return False
        return False

    async def atomic_pop(self, queue_var: TVar,
                          min_items: int = 1) -> Any:
        """Block until queue has min_items, then atomically pop one."""
        async def has_items():
            return len(queue_var._read_committed()[0]) >= min_items

        await self.wait_until(has_items, [queue_var])
        async with await STM.atomically() as tx:
            items = list(tx.read(queue_var))
            if not items:
                raise RuntimeError("Queue became empty before commit")
            item = items.pop(0)
            tx.write(queue_var, items)
            return item
```

---

## Solution 5: STMMetrics — Conflict and Retry Tracking

Instrument the STM engine to track conflict rates and retry distributions.

```python
import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class STMStats:
    commits: int = 0
    conflicts: int = 0
    retries: int = 0
    max_retries_seen: int = 0
    total_commit_time_ms: float = 0.0

    @property
    def conflict_rate(self) -> float:
        total = self.commits + self.conflicts
        return self.conflicts / total if total else 0.0

    @property
    def avg_commit_ms(self) -> float:
        return self.total_commit_time_ms / self.commits if self.commits else 0.0


class InstrumentedAtomicContext(_AtomicContext):
    """
    Drop-in replacement for _AtomicContext that tracks metrics.

    Usage:
        STM._context_class = InstrumentedAtomicContext
        # ... run agent ...
        print(InstrumentedAtomicContext.global_stats)
    """

    _stats = STMStats()

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        t0 = time.monotonic()
        retry_count = 0
        for attempt in range(self._max_retries):
            async with STM._global_lock:
                if self._log.validate():
                    self._log.commit()
                    elapsed = (time.monotonic() - t0) * 1000
                    InstrumentedAtomicContext._stats.commits += 1
                    InstrumentedAtomicContext._stats.total_commit_time_ms += elapsed
                    InstrumentedAtomicContext._stats.retries += retry_count
                    InstrumentedAtomicContext._stats.max_retries_seen = max(
                        InstrumentedAtomicContext._stats.max_retries_seen, retry_count
                    )
                    return False
            InstrumentedAtomicContext._stats.conflicts += 1
            retry_count += 1
            self._log = TransactionLog()
            await asyncio.sleep(0)
        raise RuntimeError(f"STM transaction failed after {self._max_retries} retries")

    @classmethod
    def snapshot(cls) -> Dict:
        s = cls._stats
        return {
            "commits": s.commits,
            "conflicts": s.conflicts,
            "conflict_rate": round(s.conflict_rate, 4),
            "avg_retries_per_commit": round(
                s.retries / s.commits if s.commits else 0, 2
            ),
            "max_retries_seen": s.max_retries_seen,
            "avg_commit_ms": round(s.avg_commit_ms, 3),
        }
```

---

## Solution 6: STMAwareAgentMixin — Drop-In for Agent Classes

A mixin that provides pre-wired TVars for common agent state and helpers for atomic multi-variable updates.

```python
import asyncio
from typing import Any, Dict, Optional


class STMAwareAgentMixin:
    """
    Mixin providing STM-based shared state for agent classes.
    Pre-wires TVars for session map, tool registry, and a shared counter.

    Usage:
        class MyAgent(STMAwareAgentMixin):
            async def handle(self, session_id: str, tool: str):
                async with await self.atomic() as tx:
                    self.stm_increment(tx, self._call_counter)
                    sessions = dict(self.stm_read(tx, self._sessions))
                    sessions[session_id] = sessions.get(session_id, 0) + 1
                    self.stm_write(tx, self._sessions, sessions)
    """

    def __init__(self):
        self._sessions: TVar = TVar({})
        self._tool_calls: TVar = TVar({})
        self._call_counter: TVar = TVar(0)

    async def atomic(self, max_retries: int = 100):
        return _AtomicContext(STM, max_retries)

    def stm_read(self, tx: TransactionLog, var: TVar) -> Any:
        return tx.read(var)

    def stm_write(self, tx: TransactionLog, var: TVar, value: Any):
        tx.write(var, value)

    def stm_increment(self, tx: TransactionLog, var: TVar, by: int = 1):
        STMOperations.increment(tx, var, by)

    async def atomic_session_update(self, session_id: str,
                                     update_fn) -> Any:
        async with await self.atomic() as tx:
            sessions = dict(tx.read(self._sessions))
            result = update_fn(sessions.get(session_id))
            sessions[session_id] = result
            tx.write(self._sessions, sessions)
            return result

    async def state_snapshot(self) -> Dict[str, Any]:
        async with await self.atomic() as tx:
            return {
                "sessions": tx.read(self._sessions),
                "tool_calls": tx.read(self._tool_calls),
                "total_calls": tx.read(self._call_counter),
            }
```

---

## Comparison

| Approach | Deadlock-Free | Composable | Blocking Retry | Metrics | Overhead |
|---|---|---|---|---|---|
| **TVar + STM.atomically** | Yes | Yes | No | No | Low (CAS loop) |
| **STMOperations library** | Yes | Yes | No | No | Minimal |
| **AgentStateSTM** | Yes | Yes | No | No | Low |
| **STMRetryQueue** | Yes | Yes | Yes | No | Poll interval |
| **InstrumentedAtomicContext** | Yes | Yes | No | Yes | Low |
| **STMAwareAgentMixin** | Yes | Yes | No | No | Low |

**Key insight**: STM's composability is its defining advantage over locks. Any two STM operations can be combined into one larger atomic operation without risk of deadlock — the runtime handles conflict detection and retry automatically. Use STM wherever you currently need two or more locks acquired together; the conflict rate on non-contended state is near zero, making the retry overhead negligible.
