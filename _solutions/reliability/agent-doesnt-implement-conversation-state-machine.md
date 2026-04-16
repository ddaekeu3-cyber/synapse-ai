---
title: "Agent Doesn't Implement Conversation State Machine"
description: "Agents without explicit state machines allow invalid transitions — tool calls during greeting, responses after session end, or concurrent operations that corrupt history — a formal FSM makes illegal states unrepresentable."
difficulty: intermediate
category: reliability
tags: [reliability, state-machine, fsm, conversation, lifecycle, correctness]
---

# Agent Doesn't Implement Conversation State Machine

## Problem

An agent conversation has an implicit lifecycle: greeting → active exchange → tool execution → summarization → close. Without a formal state machine, nothing prevents calling tools before context is loaded, sending a reply after the session has ended, or starting a new turn while a tool call is still in flight. These illegal transitions cause corrupted history, duplicate responses, and hard-to-reproduce bugs.

**Symptoms:**
- Tool results appended to history after a session-close clears it
- Second user message processed while first is still awaiting tool result
- Agent sends a final summary, then the user sends another message that reopens a "closed" session silently
- Concurrent requests race to write to the same history list
- No clear way to add session-level timeouts or billing checkpoints

---

## Solution 1: Minimal Enum-Based FSM

A simple enum state machine with explicit allowed transitions — illegal transitions raise immediately.

```python
import asyncio
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional
import anthropic


class ConvState(Enum):
    INIT = auto()           # Created, not yet started
    ACTIVE = auto()         # Waiting for user message
    PROCESSING = auto()     # LLM call in progress
    TOOL_PENDING = auto()   # Tool call in flight
    SUMMARIZING = auto()    # End-of-session summary
    CLOSED = auto()         # Terminal — no more transitions


# Valid transitions: {from_state: {allowed_to_states}}
TRANSITIONS: dict[ConvState, set[ConvState]] = {
    ConvState.INIT:         {ConvState.ACTIVE},
    ConvState.ACTIVE:       {ConvState.PROCESSING, ConvState.SUMMARIZING},
    ConvState.PROCESSING:   {ConvState.ACTIVE, ConvState.TOOL_PENDING, ConvState.SUMMARIZING},
    ConvState.TOOL_PENDING: {ConvState.PROCESSING},
    ConvState.SUMMARIZING:  {ConvState.CLOSED},
    ConvState.CLOSED:       set(),
}


@dataclass
class ConversationSession:
    session_id: str
    state: ConvState = ConvState.INIT
    history: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def transition(self, new_state: ConvState) -> None:
        allowed = TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Invalid transition: {self.state.name} → {new_state.name} "
                f"(allowed: {[s.name for s in allowed]})"
            )
        print(f"[fsm] {self.session_id}: {self.state.name} → {new_state.name}")
        self.state = new_state

    def assert_state(self, *expected: ConvState) -> None:
        if self.state not in expected:
            raise ValueError(
                f"Expected state {[s.name for s in expected]}, "
                f"got {self.state.name}"
            )


class StateMachineAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self._sessions: dict[str, ConversationSession] = {}

    def open_session(self, session_id: str) -> ConversationSession:
        session = ConversationSession(session_id=session_id)
        session.transition(ConvState.ACTIVE)
        self._sessions[session_id] = session
        return session

    async def send_message(self, session_id: str, user_message: str) -> str:
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError(f"Unknown session: {session_id}")

        async with session._lock:
            session.assert_state(ConvState.ACTIVE)
            session.transition(ConvState.PROCESSING)
            session.history.append({"role": "user", "content": user_message})

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=session.history,
        )
        reply = response.content[0].text

        async with session._lock:
            session.history.append({"role": "assistant", "content": reply})
            session.transition(ConvState.ACTIVE)

        return reply

    async def close_session(self, session_id: str) -> str:
        session = self._sessions[session_id]
        async with session._lock:
            session.assert_state(ConvState.ACTIVE)
            session.transition(ConvState.SUMMARIZING)

        # Generate summary
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=128,
            messages=session.history + [
                {"role": "user", "content": "Summarize this conversation in one sentence."}
            ],
        )
        summary = response.content[0].text

        async with session._lock:
            session.transition(ConvState.CLOSED)

        return summary


async def demo():
    agent = StateMachineAgent(api_key="sk-...")
    sess = agent.open_session("sess_fsm_1")

    r1 = await agent.send_message("sess_fsm_1", "Hello!")
    r2 = await agent.send_message("sess_fsm_1", "What is Python?")
    summary = await agent.close_session("sess_fsm_1")
    print(f"Summary: {summary}")

    # This should raise — session is CLOSED
    try:
        await agent.send_message("sess_fsm_1", "One more question")
    except ValueError as e:
        print(f"Correctly blocked: {e}")

# asyncio.run(demo())
```

---

## Solution 2: FSM with Entry/Exit Hooks

Attach on-enter and on-exit callbacks to each state for side effects like billing, logging, and timeout scheduling.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Coroutine, Optional
import anthropic


class State(Enum):
    IDLE = auto()
    LOADING = auto()
    ACTIVE = auto()
    TOOL_EXEC = auto()
    CLOSING = auto()
    DONE = auto()


StateHook = Callable[[str, State, State], Coroutine]  # (session_id, from, to) -> None


@dataclass
class HookedFSM:
    session_id: str
    state: State = State.IDLE
    _on_enter: dict[State, list[StateHook]] = field(default_factory=dict)
    _on_exit: dict[State, list[StateHook]] = field(default_factory=dict)
    _transitions: dict[State, set[State]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def allow(self, from_state: State, to_state: State) -> "HookedFSM":
        self._transitions.setdefault(from_state, set()).add(to_state)
        return self

    def on_enter(self, state: State, hook: StateHook) -> "HookedFSM":
        self._on_enter.setdefault(state, []).append(hook)
        return self

    def on_exit(self, state: State, hook: StateHook) -> "HookedFSM":
        self._on_exit.setdefault(state, []).append(hook)
        return self

    async def transition(self, new_state: State) -> None:
        async with self._lock:
            if new_state not in self._transitions.get(self.state, set()):
                raise ValueError(f"Illegal: {self.state.name} → {new_state.name}")

            old_state = self.state
            # Run exit hooks
            for hook in self._on_exit.get(old_state, []):
                await hook(self.session_id, old_state, new_state)

            self.state = new_state

            # Run entry hooks
            for hook in self._on_enter.get(new_state, []):
                await hook(self.session_id, old_state, new_state)


def build_conversation_fsm(session_id: str) -> HookedFSM:
    fsm = HookedFSM(session_id=session_id)

    # Define transitions
    (fsm
        .allow(State.IDLE,     State.LOADING)
        .allow(State.LOADING,  State.ACTIVE)
        .allow(State.ACTIVE,   State.TOOL_EXEC)
        .allow(State.ACTIVE,   State.CLOSING)
        .allow(State.TOOL_EXEC,State.ACTIVE)
        .allow(State.CLOSING,  State.DONE)
    )

    # Billing: charge on session open
    async def on_session_open(sid, frm, to):
        print(f"[billing] Session {sid} opened — start billing timer")

    # Logging: log every state entry
    async def on_any_enter(sid, frm, to):
        print(f"[log] {sid}: entered {to.name} from {frm.name}")

    for s in State:
        fsm.on_enter(s, on_any_enter)

    fsm.on_enter(State.LOADING, on_session_open)

    # Billing: stop timer on DONE
    async def on_done(sid, frm, to):
        print(f"[billing] Session {sid} closed — billing stopped")

    fsm.on_enter(State.DONE, on_done)
    return fsm


class HookedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run_session(self, session_id: str, messages: list[str]) -> list[str]:
        fsm = build_conversation_fsm(session_id)
        await fsm.transition(State.LOADING)
        await fsm.transition(State.ACTIVE)

        history: list[dict] = []
        replies = []

        for msg in messages:
            history.append({"role": "user", "content": msg})
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=128,
                messages=history,
            )
            reply = response.content[0].text
            history.append({"role": "assistant", "content": reply})
            replies.append(reply)

        await fsm.transition(State.CLOSING)
        await fsm.transition(State.DONE)
        return replies


async def demo():
    agent = HookedAgent(api_key="sk-...")
    replies = await agent.run_session("sess_hooked", ["Hello!", "What is an FSM?"])
    for r in replies:
        print(r[:60])

# asyncio.run(demo())
```

---

## Solution 3: Concurrent-Safe FSM with Condition Variable

Prevent concurrent state corruption using `asyncio.Condition` — transitions block until the current state operation completes.

```python
import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import anthropic


class Phase(Enum):
    IDLE = auto()
    USER_TURN = auto()
    LLM_PROCESSING = auto()
    TOOL_RUNNING = auto()
    FINISHED = auto()


@dataclass
class ConcurrentSafeFSM:
    _state: Phase = Phase.IDLE
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    @property
    def state(self) -> Phase:
        return self._state

    async def acquire_for_transition(
        self, expected: Phase, next_phase: Phase
    ) -> None:
        async with self._condition:
            # Wait until state matches expected
            await self._condition.wait_for(lambda: self._state == expected)
            self._state = next_phase
            self._condition.notify_all()

    async def set(self, new_phase: Phase) -> None:
        async with self._condition:
            self._state = new_phase
            self._condition.notify_all()

    async def wait_until(self, target: Phase) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._state == target)


class ConcurrentSafeAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.fsm = ConcurrentSafeFSM()
        self.history: list[dict] = []

    async def start(self) -> None:
        await self.fsm.set(Phase.USER_TURN)

    async def send(self, message: str) -> str:
        # Blocks if another turn is in progress
        await self.fsm.acquire_for_transition(Phase.USER_TURN, Phase.LLM_PROCESSING)
        self.history.append({"role": "user", "content": message})

        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=self.history,
            )
            reply = response.content[0].text
            self.history.append({"role": "assistant", "content": reply})
            return reply
        finally:
            await self.fsm.set(Phase.USER_TURN)

    async def close(self) -> None:
        await self.fsm.set(Phase.FINISHED)


async def demo():
    agent = ConcurrentSafeAgent(api_key="sk-...")
    await agent.start()

    # Try to send two messages concurrently — second blocks until first completes
    results = await asyncio.gather(
        agent.send("First message"),
        agent.send("Second message"),
    )
    for r in results:
        print(r[:60])
    await agent.close()

# asyncio.run(demo())
```

---

## Solution 4: Hierarchical FSM for Multi-Agent Orchestration

A parent FSM manages sub-agent lifecycles; each sub-agent has its own nested FSM.

```python
import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import anthropic


class OrchestratorState(Enum):
    SPAWNING = auto()
    DELEGATING = auto()
    COLLECTING = auto()
    SYNTHESIZING = auto()
    DONE = auto()


class SubAgentState(Enum):
    ASSIGNED = auto()
    RUNNING = auto()
    COMPLETE = auto()
    FAILED = auto()


@dataclass
class SubAgentFSM:
    agent_id: str
    task: str
    state: SubAgentState = SubAgentState.ASSIGNED
    result: Optional[str] = None
    error: Optional[str] = None


@dataclass
class OrchestratorFSM:
    state: OrchestratorState = OrchestratorState.SPAWNING
    sub_agents: list[SubAgentFSM] = field(default_factory=list)

    def all_complete(self) -> bool:
        return all(
            a.state in (SubAgentState.COMPLETE, SubAgentState.FAILED)
            for a in self.sub_agents
        )

    def results(self) -> list[str]:
        return [a.result for a in self.sub_agents if a.result is not None]


class HierarchicalOrchestrator:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def _run_sub_agent(self, fsm: SubAgentFSM) -> None:
        fsm.state = SubAgentState.RUNNING
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=128,
                messages=[{"role": "user", "content": fsm.task}],
            )
            fsm.result = response.content[0].text
            fsm.state = SubAgentState.COMPLETE
        except Exception as exc:
            fsm.error = str(exc)
            fsm.state = SubAgentState.FAILED
            print(f"[fsm] Sub-agent {fsm.agent_id} FAILED: {exc}")

    async def run(self, tasks: list[str]) -> str:
        orch = OrchestratorFSM()

        # SPAWNING → create sub-agent FSMs
        for i, task in enumerate(tasks):
            orch.sub_agents.append(SubAgentFSM(agent_id=f"sub_{i}", task=task))
        orch.state = OrchestratorState.DELEGATING

        # DELEGATING → run all sub-agents concurrently
        print(f"[fsm] Orchestrator: DELEGATING to {len(orch.sub_agents)} sub-agents")
        await asyncio.gather(*(self._run_sub_agent(a) for a in orch.sub_agents))
        orch.state = OrchestratorState.COLLECTING

        # COLLECTING → verify all done
        assert orch.all_complete(), "Some sub-agents still running"
        orch.state = OrchestratorState.SYNTHESIZING

        # SYNTHESIZING → merge results
        combined = "\n".join(f"[{i}] {r}" for i, r in enumerate(orch.results()))
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Synthesize these findings:\n{combined}"}],
        )
        orch.state = OrchestratorState.DONE
        print(f"[fsm] Orchestrator: DONE")
        return response.content[0].text


async def demo():
    orch = HierarchicalOrchestrator(api_key="sk-...")
    result = await orch.run([
        "What are the benefits of Python?",
        "What are the drawbacks of Python?",
        "What alternatives exist to Python?",
    ])
    print(result[:200])

# asyncio.run(demo())
```

---

## Solution 5: Timeout-Aware FSM with Auto-Transition

Automatically transition to a timeout state if a phase takes too long, enabling graceful degradation.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import anthropic


class SessionPhase(Enum):
    ACTIVE = auto()
    PROCESSING = auto()
    TIMED_OUT = auto()
    DONE = auto()


@dataclass
class TimedFSM:
    session_id: str
    state: SessionPhase = SessionPhase.ACTIVE
    phase_started: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def elapsed(self) -> float:
        return time.monotonic() - self.phase_started

    async def transition(self, new_state: SessionPhase) -> None:
        async with self._lock:
            self.state = new_state
            self.phase_started = time.monotonic()
            print(f"[fsm] {self.session_id}: → {new_state.name}")


class TimeoutAwareFSM:
    def __init__(self, api_key: str, processing_timeout: float = 10.0):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.timeout = processing_timeout

    async def process_turn(
        self, session_id: str, message: str, history: list[dict]
    ) -> tuple[str, bool]:
        fsm = TimedFSM(session_id=session_id)
        await fsm.transition(SessionPhase.PROCESSING)
        history.append({"role": "user", "content": message})

        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=256,
                    messages=history,
                ),
                timeout=self.timeout,
            )
            reply = response.content[0].text
            history.append({"role": "assistant", "content": reply})
            await fsm.transition(SessionPhase.ACTIVE)
            return reply, False

        except asyncio.TimeoutError:
            await fsm.transition(SessionPhase.TIMED_OUT)
            print(f"[fsm] {session_id}: processing timed out after {self.timeout}s")
            history.pop()  # Remove unprocessed user message
            return "I'm taking too long to respond. Please try again.", True

        finally:
            if fsm.state == SessionPhase.PROCESSING:
                await fsm.transition(SessionPhase.DONE)


async def demo():
    agent = TimeoutAwareFSM(api_key="sk-...", processing_timeout=5.0)
    history: list[dict] = []
    reply, timed_out = await agent.process_turn("sess_timer", "Hello!", history)
    print(f"Reply: {reply[:60]} (timed_out={timed_out})")

# asyncio.run(demo())
```

---

## Solution 6: Serializable FSM for Persistence and Replay

Serialize the full FSM state to JSON for durable storage; deserialize and resume from any checkpoint.

```python
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional
import anthropic


class ConversationPhase(str, Enum):
    INIT = "init"
    ACTIVE = "active"
    TOOL_PENDING = "tool_pending"
    CLOSED = "closed"


@dataclass
class SerializableFSM:
    session_id: str
    phase: ConversationPhase = ConversationPhase.INIT
    history: list[dict] = field(default_factory=list)
    tool_calls_made: int = 0
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "session_id": self.session_id,
            "phase": self.phase.value,
            "history": self.history,
            "tool_calls_made": self.tool_calls_made,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
        })

    @classmethod
    def from_json(cls, raw: str) -> "SerializableFSM":
        data = json.loads(raw)
        fsm = cls(
            session_id=data["session_id"],
            phase=ConversationPhase(data["phase"]),
            history=data["history"],
            tool_calls_made=data["tool_calls_made"],
            created_at=data["created_at"],
            last_updated=data["last_updated"],
        )
        return fsm

    def transition(self, new_phase: ConversationPhase) -> None:
        valid = {
            ConversationPhase.INIT:         {ConversationPhase.ACTIVE},
            ConversationPhase.ACTIVE:       {ConversationPhase.TOOL_PENDING, ConversationPhase.CLOSED},
            ConversationPhase.TOOL_PENDING: {ConversationPhase.ACTIVE},
            ConversationPhase.CLOSED:       set(),
        }
        if new_phase not in valid.get(self.phase, set()):
            raise ValueError(f"Illegal: {self.phase} → {new_phase}")
        self.phase = new_phase
        self.last_updated = time.time()


class PersistentFSMAgent:
    def __init__(self, api_key: str, storage: dict):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.storage = storage  # Dict simulating Redis/Postgres

    def _load(self, session_id: str) -> SerializableFSM:
        if session_id in self.storage:
            return SerializableFSM.from_json(self.storage[session_id])
        fsm = SerializableFSM(session_id=session_id)
        fsm.transition(ConversationPhase.ACTIVE)
        return fsm

    def _save(self, fsm: SerializableFSM) -> None:
        self.storage[fsm.session_id] = fsm.to_json()

    async def chat(self, session_id: str, message: str) -> str:
        fsm = self._load(session_id)

        if fsm.phase != ConversationPhase.ACTIVE:
            raise ValueError(f"Session {session_id} not ACTIVE: {fsm.phase}")

        fsm.history.append({"role": "user", "content": message})
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=fsm.history,
        )
        reply = response.content[0].text
        fsm.history.append({"role": "assistant", "content": reply})
        self._save(fsm)
        return reply

    def close(self, session_id: str) -> None:
        fsm = self._load(session_id)
        fsm.transition(ConversationPhase.CLOSED)
        self._save(fsm)
        print(f"[fsm] {session_id} closed after {len(fsm.history)//2} turns")


async def demo():
    storage: dict = {}
    agent = PersistentFSMAgent(api_key="sk-...", storage=storage)

    # Turn 1
    r1 = await agent.chat("sess_persist", "Hello!")
    print(f"T1: {r1[:40]}")

    # Simulate process restart — reload from storage
    r2 = await agent.chat("sess_persist", "What did I say before?")
    print(f"T2: {r2[:40]}")

    agent.close("sess_persist")

    try:
        await agent.chat("sess_persist", "One more?")
    except ValueError as e:
        print(f"Blocked after close: {e}")

# asyncio.run(demo())
```

---

## Comparison

| Solution | Concurrency Safe | Serializable | Entry/Exit Hooks | Timeout | Nested FSM | Complexity |
|---|---|---|---|---|---|---|
| Enum + allowed transitions | With lock | No | No | No | No | Very Low |
| Hooked FSM with callbacks | With lock | No | Yes | No | No | Low |
| Condition-variable FSM | Yes | No | No | No | No | Medium |
| Hierarchical orchestrator | Yes | No | No | No | Yes | Medium |
| Timeout-aware FSM | With lock | No | No | Yes | No | Low |
| Serializable FSM | Manual | Yes | No | No | No | Low |

**Recommendation:** Start with Solution 1 (enum + transition table) — it's five lines of code that makes illegal states a `ValueError` instead of a silent bug. Add Solution 2 (entry/exit hooks) for billing, logging, and alerting side effects. Use Solution 6 (serializable FSM) when you need session durability across process restarts.
