---
title: "Agent Doesn't Implement Two-Phase Commit for Distributed Writes"
description: "How to coordinate atomic writes across multiple services, databases, or agents using two-phase commit (2PC), three-phase commit, and compensating transactions to prevent partial updates and data divergence."
date: 2025-01-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-two-phase-commit-for-distributed-writes
tags:
  - reliability
  - distributed-systems
  - two-phase-commit
  - atomicity
  - consistency
  - transactions
  - saga
symptoms:
  - "Agent writes to database A and message queue B, but crashes between the two writes"
  - "Tool call results stored in memory store but billing event never emitted"
  - "Partial updates visible to users when any participant in a multi-step write fails"
  - "No rollback mechanism when the second of two coordinated writes fails"
  - "Distributed state diverges because there is no coordinator to enforce atomicity"
  - "Retry logic re-executes committed writes causing duplicates"
---

## Why This Happens

AI agents frequently need to write to multiple systems atomically: store a conversation result *and* emit a billing event, update a knowledge base *and* invalidate a cache, write to a primary database *and* replicate to a secondary. Without coordination, these writes can partially succeed — leaving the system in an inconsistent state that is neither the old nor the new value.

Two-phase commit (2PC) solves this by introducing a coordinator that ensures all participants either commit or roll back together. While 2PC has well-known availability trade-offs, it remains the right tool when strict atomicity across heterogeneous systems is required and partition tolerance can be sacrificed. For higher availability, the Saga pattern with compensating transactions provides eventual consistency.

---

## Solution 1: Classic Two-Phase Commit Coordinator

The 2PC coordinator drives two rounds: *prepare* (can you commit?) and *commit* (do commit). Any prepare failure triggers a global abort.

```python
import asyncio
import uuid
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

class TransactionState(Enum):
    INITIAL    = "initial"
    PREPARING  = "preparing"
    PREPARED   = "prepared"
    COMMITTING = "committing"
    COMMITTED  = "committed"
    ABORTING   = "aborting"
    ABORTED    = "aborted"

@dataclass
class ParticipantVote:
    participant_id: str
    vote: str  # "yes" or "no"
    reason: str = ""

class TwoPhaseCommitParticipant:
    """Interface every participant must implement."""

    async def prepare(self, txn_id: str, payload: Any) -> ParticipantVote:
        raise NotImplementedError

    async def commit(self, txn_id: str) -> bool:
        raise NotImplementedError

    async def abort(self, txn_id: str) -> bool:
        raise NotImplementedError


@dataclass
class TransactionRecord:
    txn_id: str
    state: TransactionState
    participants: list[str]
    votes: list[ParticipantVote] = field(default_factory=list)
    committed_participants: list[str] = field(default_factory=list)
    aborted_participants: list[str] = field(default_factory=list)


class TwoPhaseCommitCoordinator:
    """
    Durable 2PC coordinator.
    Writes transaction state to a WAL so recovery is possible after coordinator crash.
    """

    def __init__(self, wal=None):
        self._transactions: dict[str, TransactionRecord] = {}
        self._participants: dict[str, TwoPhaseCommitParticipant] = {}
        self._wal = wal  # Optional WriteAheadLog for durability

    def register_participant(self, pid: str, participant: TwoPhaseCommitParticipant) -> None:
        self._participants[pid] = participant

    def _log(self, txn_id: str, event: str, data: Any = None) -> None:
        if self._wal:
            self._wal.write_intent(event, {"txn_id": txn_id, "data": data}, operation_id=f"{txn_id}:{event}")
        logger.debug("[2PC] %s txn=%s data=%s", event, txn_id, data)

    async def execute(
        self,
        payloads: dict[str, Any],  # {participant_id: payload}
    ) -> bool:
        """
        Execute a distributed transaction across all participants.
        Returns True if committed, False if aborted.
        """
        txn_id = str(uuid.uuid4())
        participant_ids = list(payloads.keys())
        record = TransactionRecord(
            txn_id=txn_id,
            state=TransactionState.PREPARING,
            participants=participant_ids,
        )
        self._transactions[txn_id] = record
        self._log(txn_id, "START", {"participants": participant_ids})

        # --- Phase 1: Prepare ---
        prepare_tasks = {
            pid: asyncio.create_task(
                self._participants[pid].prepare(txn_id, payload)
            )
            for pid, payload in payloads.items()
            if pid in self._participants
        }

        votes: list[ParticipantVote] = []
        for pid, task in prepare_tasks.items():
            try:
                vote = await task
                votes.append(vote)
            except Exception as exc:
                votes.append(ParticipantVote(pid, "no", str(exc)))

        record.votes = votes
        all_yes = all(v.vote == "yes" for v in votes)

        if all_yes:
            record.state = TransactionState.COMMITTING
            self._log(txn_id, "COMMIT_DECISION")
            return await self._phase2_commit(record)
        else:
            no_voters = [v.participant_id for v in votes if v.vote == "no"]
            record.state = TransactionState.ABORTING
            self._log(txn_id, "ABORT_DECISION", {"no_voters": no_voters})
            await self._phase2_abort(record)
            return False

    async def _phase2_commit(self, record: TransactionRecord) -> bool:
        commit_tasks = {
            pid: asyncio.create_task(self._participants[pid].commit(record.txn_id))
            for pid in record.participants
            if pid in self._participants
        }
        all_committed = True
        for pid, task in commit_tasks.items():
            try:
                success = await task
                if success:
                    record.committed_participants.append(pid)
                else:
                    all_committed = False
                    logger.error("[2PC] Commit failed for participant %s txn=%s", pid, record.txn_id)
            except Exception as exc:
                all_committed = False
                logger.error("[2PC] Commit exception for %s txn=%s: %s", pid, record.txn_id, exc)

        record.state = TransactionState.COMMITTED if all_committed else TransactionState.ABORTING
        return all_committed

    async def _phase2_abort(self, record: TransactionRecord) -> None:
        abort_tasks = [
            asyncio.create_task(self._participants[pid].abort(record.txn_id))
            for pid in record.participants
            if pid in self._participants
        ]
        await asyncio.gather(*abort_tasks, return_exceptions=True)
        record.state = TransactionState.ABORTED

    async def recover(self) -> None:
        """On coordinator restart, complete any in-flight transactions."""
        for record in self._transactions.values():
            if record.state == TransactionState.COMMITTING:
                logger.warning("[2PC] Recovery: re-sending commit for txn %s", record.txn_id)
                await self._phase2_commit(record)
            elif record.state in (TransactionState.ABORTING, TransactionState.PREPARING):
                logger.warning("[2PC] Recovery: re-sending abort for txn %s", record.txn_id)
                await self._phase2_abort(record)
```

---

## Solution 2: Concrete Participants — Database and Message Queue

Real participant implementations for a database store and a message queue.

```python
import asyncio
from typing import Any

class InMemoryDatabaseParticipant(TwoPhaseCommitParticipant):
    """Simulates a database that supports prepare/commit/abort."""

    def __init__(self, name: str):
        self.name = name
        self._committed: dict[str, Any] = {}
        self._pending: dict[str, Any] = {}  # txn_id -> prepared payload

    async def prepare(self, txn_id: str, payload: Any) -> ParticipantVote:
        # Validate payload and lock resources
        if "key" not in payload:
            return ParticipantVote(self.name, "no", "missing 'key' in payload")
        # Write to pending journal
        self._pending[txn_id] = payload
        return ParticipantVote(self.name, "yes")

    async def commit(self, txn_id: str) -> bool:
        payload = self._pending.pop(txn_id, None)
        if payload is None:
            return False  # idempotent — already committed or never prepared
        self._committed[payload["key"]] = payload["value"]
        return True

    async def abort(self, txn_id: str) -> bool:
        self._pending.pop(txn_id, None)
        return True

    def get(self, key: str) -> Any:
        return self._committed.get(key)


class MessageQueueParticipant(TwoPhaseCommitParticipant):
    """
    Simulates a transactional message queue (e.g., Kafka transactions).
    Messages are held in a staging area until commit.
    """

    def __init__(self, name: str):
        self.name = name
        self._staging: dict[str, list] = {}  # txn_id -> [messages]
        self._published: list = []

    async def prepare(self, txn_id: str, payload: Any) -> ParticipantVote:
        if not isinstance(payload, dict) or "messages" not in payload:
            return ParticipantVote(self.name, "no", "payload must have 'messages'")
        self._staging[txn_id] = payload["messages"]
        return ParticipantVote(self.name, "yes")

    async def commit(self, txn_id: str) -> bool:
        messages = self._staging.pop(txn_id, None)
        if messages is None:
            return True  # idempotent
        self._published.extend(messages)
        return True

    async def abort(self, txn_id: str) -> bool:
        self._staging.pop(txn_id, None)
        return True


# --- Example: atomic DB write + event publish ---

async def demo_2pc():
    coordinator = TwoPhaseCommitCoordinator()
    db = InMemoryDatabaseParticipant("primary_db")
    mq = MessageQueueParticipant("event_bus")

    coordinator.register_participant("db", db)
    coordinator.register_participant("mq", mq)

    success = await coordinator.execute({
        "db": {"key": "user:42:balance", "value": 1000},
        "mq": {"messages": [{"type": "balance_updated", "user": 42, "amount": 1000}]},
    })

    print(f"Committed: {success}")
    print(f"DB value: {db.get('user:42:balance')}")
    print(f"MQ messages: {mq._published}")
```

---

## Solution 3: Three-Phase Commit for Reduced Blocking

3PC adds a *pre-commit* phase between prepare and commit, allowing participants to safely abort even if the coordinator fails after the commit decision — eliminating the 2PC blocking problem.

```python
class ThreePhaseParticipant:
    async def prepare(self, txn_id: str, payload: Any) -> str:  # "yes"/"no"
        raise NotImplementedError

    async def pre_commit(self, txn_id: str) -> bool:
        """Move to pre-committed state. Safe to commit even if coordinator dies."""
        raise NotImplementedError

    async def commit(self, txn_id: str) -> bool:
        raise NotImplementedError

    async def abort(self, txn_id: str) -> bool:
        raise NotImplementedError

class ThreePhaseCoordinator:
    def __init__(self):
        self._participants: dict[str, ThreePhaseParticipant] = {}

    def register(self, pid: str, p: ThreePhaseParticipant) -> None:
        self._participants[pid] = p

    async def execute(self, payloads: dict[str, Any]) -> bool:
        txn_id = str(uuid.uuid4())

        # Phase 1: Can commit?
        prepare_results = await asyncio.gather(
            *[p.prepare(txn_id, payloads.get(pid, {})) for pid, p in self._participants.items()],
            return_exceptions=True,
        )
        if any(r != "yes" or isinstance(r, Exception) for r in prepare_results):
            await asyncio.gather(*[p.abort(txn_id) for p in self._participants.values()], return_exceptions=True)
            return False

        # Phase 2: Pre-commit (participants reach a state from which they can commit safely)
        pre_results = await asyncio.gather(
            *[p.pre_commit(txn_id) for p in self._participants.values()],
            return_exceptions=True,
        )
        if any(r is not True or isinstance(r, Exception) for r in pre_results):
            await asyncio.gather(*[p.abort(txn_id) for p in self._participants.values()], return_exceptions=True)
            return False

        # Phase 3: Commit
        commit_results = await asyncio.gather(
            *[p.commit(txn_id) for p in self._participants.values()],
            return_exceptions=True,
        )
        return all(r is True for r in commit_results)
```

---

## Solution 4: Saga with Compensating Transactions

For long-running workflows where 2PC latency is unacceptable, the Saga pattern executes each step independently and compensates (undoes) previous steps on failure.

```python
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class SagaStep:
    name: str
    action: Callable[[dict], Awaitable[dict]]        # forward action
    compensate: Callable[[dict], Awaitable[None]]     # undo action
    rollback_on_failure: bool = True

class SagaExecutor:
    """
    Executes saga steps sequentially; on failure, runs compensating transactions
    in reverse order for all completed steps.
    """

    def __init__(self):
        self._completed: list[tuple[SagaStep, dict]] = []

    async def run(self, steps: list[SagaStep], initial_context: dict) -> dict:
        context = dict(initial_context)
        self._completed.clear()

        for step in steps:
            logger.info("[SAGA] Executing step: %s", step.name)
            try:
                result = await step.action(context)
                context.update(result or {})
                self._completed.append((step, dict(context)))
            except Exception as exc:
                logger.error("[SAGA] Step '%s' failed: %s — rolling back", step.name, exc)
                if step.rollback_on_failure:
                    await self._rollback(context)
                raise RuntimeError(f"Saga aborted at step '{step.name}': {exc}") from exc

        return context

    async def _rollback(self, context: dict) -> None:
        for step, step_context in reversed(self._completed):
            try:
                await step.compensate(step_context)
                logger.info("[SAGA] Compensated step: %s", step.name)
            except Exception as exc:
                logger.error("[SAGA] Compensation failed for '%s': %s", step.name, exc)


# --- Example: agent payment + fulfillment saga ---

async def demo_saga():
    executor = SagaExecutor()

    async def reserve_inventory(ctx):
        # Reserve items
        return {"inventory_reservation_id": "res-123"}

    async def release_inventory(ctx):
        # Release reservation
        logger.info("Releasing inventory reservation %s", ctx.get("inventory_reservation_id"))

    async def charge_payment(ctx):
        return {"payment_id": "pay-456", "amount": 99.99}

    async def refund_payment(ctx):
        logger.info("Refunding payment %s", ctx.get("payment_id"))

    async def send_confirmation(ctx):
        return {"email_sent": True}

    async def cancel_confirmation(ctx):
        pass  # Cannot un-send email, but log it

    steps = [
        SagaStep("reserve_inventory", reserve_inventory, release_inventory),
        SagaStep("charge_payment",    charge_payment,    refund_payment),
        SagaStep("send_confirmation", send_confirmation, cancel_confirmation),
    ]

    try:
        result = await executor.run(steps, {"user_id": 42, "product_id": "P001"})
        print("Saga completed:", result)
    except RuntimeError as e:
        print("Saga rolled back:", e)
```

---

## Solution 5: Outbox Pattern as 2PC Alternative

For database + message queue atomicity without 2PC, write to an outbox table in the same database transaction. A relay process publishes outbox messages to the queue.

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class OutboxMessage:
    id: str
    aggregate_id: str
    event_type: str
    payload: dict
    created_at: float = field(default_factory=time.time)
    published: bool = False

class OutboxStore:
    """In-memory simulation of an outbox table in the main database."""

    def __init__(self):
        self._messages: dict[str, OutboxMessage] = {}
        self._state: dict[str, Any] = {}

    def write_with_outbox(
        self,
        key: str,
        value: Any,
        event_type: str,
        event_payload: dict,
    ) -> str:
        """
        Write state + outbox message in a single atomic database transaction.
        This guarantees the event is recorded even if the publisher crashes.
        """
        msg_id = str(uuid.uuid4())
        # Simulate atomic database transaction
        self._state[key] = value
        self._messages[msg_id] = OutboxMessage(
            id=msg_id,
            aggregate_id=key,
            event_type=event_type,
            payload=event_payload,
        )
        return msg_id

    def get_unpublished(self) -> list[OutboxMessage]:
        return [m for m in self._messages.values() if not m.published]

    def mark_published(self, msg_id: str) -> None:
        if msg_id in self._messages:
            self._messages[msg_id].published = True


class OutboxRelay:
    """
    Polls the outbox and publishes messages to the target queue.
    Idempotent — safe to run multiple times.
    """

    def __init__(self, store: OutboxStore, publisher, poll_interval: float = 1.0):
        self.store = store
        self.publisher = publisher
        self.poll_interval = poll_interval
        self._running = False

    async def start(self) -> None:
        self._running = True
        while self._running:
            await self._relay_batch()
            await asyncio.sleep(self.poll_interval)

    async def _relay_batch(self) -> None:
        messages = self.store.get_unpublished()
        for msg in messages:
            try:
                await self.publisher.publish(msg.event_type, msg.payload)
                self.store.mark_published(msg.id)
            except Exception as exc:
                logger.warning("Failed to relay outbox message %s: %s", msg.id, exc)

    def stop(self) -> None:
        self._running = False
```

---

## Solution 6: Transaction Manager with Timeout and Deadlock Detection

A production-ready transaction manager that enforces timeouts, detects stuck transactions, and triggers recovery.

```python
import asyncio
import time
from typing import Optional

class TransactionManager:
    """
    Wraps the 2PC coordinator with timeout enforcement and stuck-transaction recovery.
    """

    def __init__(
        self,
        coordinator: TwoPhaseCommitCoordinator,
        default_timeout: float = 30.0,
    ):
        self.coordinator = coordinator
        self.default_timeout = default_timeout
        self._active: dict[str, float] = {}  # txn_id -> start_time
        self._monitor_task: Optional[asyncio.Task] = None

    def start_monitoring(self) -> None:
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def execute_with_timeout(
        self,
        payloads: dict[str, Any],
        timeout: Optional[float] = None,
    ) -> bool:
        timeout = timeout or self.default_timeout
        txn_id = str(uuid.uuid4())
        self._active[txn_id] = time.monotonic()
        try:
            return await asyncio.wait_for(
                self.coordinator.execute(payloads),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error("Transaction %s timed out after %.1fs", txn_id, timeout)
            # Trigger abort for all participants
            for pid, participant in self.coordinator._participants.items():
                try:
                    await participant.abort(txn_id)
                except Exception:
                    pass
            return False
        finally:
            self._active.pop(txn_id, None)

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            now = time.monotonic()
            stuck = [
                txn_id for txn_id, start in self._active.items()
                if now - start > self.default_timeout * 2
            ]
            for txn_id in stuck:
                logger.warning("Stuck transaction detected: %s — forcing abort", txn_id)
                self._active.pop(txn_id, None)
```

---

## Comparison

| Solution | Consistency | Availability | Blocking | Best For |
|---|---|---|---|---|
| Classic 2PC | Strong | Low (coordinator SPOF) | Yes | Same-DC multi-system writes |
| Three-Phase Commit | Strong | Better | Reduced | Distributed writes tolerating extra RTT |
| Saga + Compensation | Eventual | High | No | Long-running cross-service workflows |
| Outbox Pattern | Strong (DB scope) | High | No | DB + message queue atomicity |
| Transaction Manager | Strong | Medium | Yes | Timeout-bounded 2PC in production |
| In-Memory Participants | N/A (demo) | N/A | No | Testing 2PC logic without real stores |

**Use 2PC** when you need strong atomicity across two systems in the same data center and can tolerate coordinator blocking. **Use 3PC** when network partitions are possible and blocking is unacceptable. **Use Saga** for long-running business workflows spanning multiple services — e.g., order fulfillment across inventory, payment, and shipping. **Use the Outbox pattern** as a simpler, highly available alternative to 2PC for database + message-queue atomicity. Always add the **Transaction Manager** timeout wrapper to prevent indefinitely blocked participants.
