---
layout: solution
title: "Agent Doesn't Implement Multi-Turn Conversation State Machine"
category: general
description: "Model conversation flow as an explicit state machine so the agent always knows what phase it's in, what transitions are valid, and how to recover from unexpected inputs."
tags: [general, state-machine, multi-turn, conversation, flow-control, python]
---

# Agent Doesn't Implement Multi-Turn Conversation State Machine

Agents without explicit conversation states drift: they ask clarifying questions in the middle of execution, forget which step they're on, or accept inputs that should be invalid at that point. A state machine makes the conversation flow explicit, testable, and recoverable.

## Option 1: Enum-Based State Machine with Transition Guards

```python
import anthropic
from enum import Enum, auto
from dataclasses import dataclass, field

client = anthropic.Anthropic()

class State(Enum):
    GREETING     = auto()
    GATHER_TOPIC = auto()
    GATHER_DEPTH = auto()
    GENERATING   = auto()
    REVIEW       = auto()
    DONE         = auto()

VALID_TRANSITIONS: dict[State, list[State]] = {
    State.GREETING:     [State.GATHER_TOPIC],
    State.GATHER_TOPIC: [State.GATHER_DEPTH],
    State.GATHER_DEPTH: [State.GENERATING],
    State.GENERATING:   [State.REVIEW],
    State.REVIEW:       [State.DONE, State.GATHER_TOPIC],
    State.DONE:         [],
}

@dataclass
class ConversationContext:
    state: State = State.GREETING
    topic: str = ""
    depth: str = ""
    draft: str = ""
    history: list[dict] = field(default_factory=list)

    def transition(self, next_state: State):
        if next_state not in VALID_TRANSITIONS[self.state]:
            raise ValueError(f"Invalid transition: {self.state} -> {next_state}")
        print(f"  [FSM] {self.state.name} -> {next_state.name}")
        self.state = next_state

def agent_reply(ctx: ConversationContext, user_input: str) -> str:
    ctx.history.append({"role": "user", "content": user_input})

    if ctx.state == State.GREETING:
        ctx.transition(State.GATHER_TOPIC)
        reply = "Hello! What topic would you like me to explain?"

    elif ctx.state == State.GATHER_TOPIC:
        ctx.topic = user_input.strip()
        ctx.transition(State.GATHER_DEPTH)
        reply = f"Great — '{ctx.topic}'. How deep should I go? (brief / detailed / expert)"

    elif ctx.state == State.GATHER_DEPTH:
        ctx.depth = user_input.strip()
        ctx.transition(State.GENERATING)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Explain '{ctx.topic}' at a '{ctx.depth}' level."
            }],
        )
        ctx.draft = resp.content[0].text
        ctx.transition(State.REVIEW)
        reply = f"{ctx.draft}\n\n---\nAre you satisfied? (yes / no, try again)"

    elif ctx.state == State.REVIEW:
        if user_input.strip().lower().startswith("yes"):
            ctx.transition(State.DONE)
            reply = "Great! Let me know if you need anything else."
        else:
            ctx.transition(State.GATHER_TOPIC)
            reply = "No problem! What topic should we try instead?"

    else:
        reply = "Conversation complete. Start a new session to continue."

    ctx.history.append({"role": "assistant", "content": reply})
    return reply

ctx = ConversationContext()
for turn in ["Hi", "asyncio in Python", "brief", "yes"]:
    print(f"User: {turn}")
    print(f"Agent: {agent_reply(ctx, turn)}\n")

# Expected Token Savings: Single-shot generation per state; no re-prompting or context drift
# Environment: pure Python; extend VALID_TRANSITIONS for branching flows
```

## Option 2: State Machine with Timeout and Re-Prompt

```python
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class Phase(Enum):
    INIT        = "init"
    COLLECTING  = "collecting"
    CONFIRMING  = "confirming"
    EXECUTING   = "executing"
    COMPLETE    = "complete"
    TIMED_OUT   = "timed_out"

PHASE_TIMEOUT_S = {
    Phase.COLLECTING: 300,
    Phase.CONFIRMING: 60,
}

@dataclass
class Session:
    phase: Phase = Phase.INIT
    data: dict = field(default_factory=dict)
    phase_entered_at: float = field(default_factory=time.time)
    reprompt_count: int = 0
    MAX_REPROMPTS: int = 2

    def enter(self, phase: Phase):
        self.phase = phase
        self.phase_entered_at = time.time()
        self.reprompt_count = 0

    def is_timed_out(self) -> bool:
        limit = PHASE_TIMEOUT_S.get(self.phase, float("inf"))
        return (time.time() - self.phase_entered_at) > limit

def step(session: Session, user_input: str | None) -> str:
    # Check timeout before processing input
    if session.is_timed_out():
        session.enter(Phase.TIMED_OUT)
        return "Session timed out waiting for your response. Please start over."

    if session.phase == Phase.INIT:
        session.enter(Phase.COLLECTING)
        return "What task would you like me to perform?"

    elif session.phase == Phase.COLLECTING:
        if not user_input or len(user_input.strip()) < 3:
            session.reprompt_count += 1
            if session.reprompt_count > session.MAX_REPROMPTS:
                session.enter(Phase.TIMED_OUT)
                return "Too many empty inputs. Session ended."
            return "Please describe the task in more detail."
        session.data["task"] = user_input.strip()
        session.enter(Phase.CONFIRMING)
        return f"You want me to: '{session.data['task']}'. Confirm? (yes/no)"

    elif session.phase == Phase.CONFIRMING:
        answer = (user_input or "").strip().lower()
        if answer.startswith("y"):
            session.enter(Phase.EXECUTING)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": session.data["task"]}],
            )
            result = resp.content[0].text
            session.data["result"] = result
            session.enter(Phase.COMPLETE)
            return f"Done!\n\n{result}"
        elif answer.startswith("n"):
            session.enter(Phase.COLLECTING)
            return "Okay, what should I do instead?"
        else:
            session.reprompt_count += 1
            if session.reprompt_count > session.MAX_REPROMPTS:
                session.enter(Phase.COLLECTING)
                return "I'll take that as a no. What would you like to do?"
            return "Please answer yes or no."

    return f"[{session.phase.value}] No handler for this phase."

session = Session()
turns = [None, "Summarize the CAP theorem in 2 sentences", "yes"]
for user_input in turns:
    if user_input:
        print(f"User: {user_input}")
    response = step(session, user_input)
    print(f"Agent [{session.phase.value}]: {response}\n")

# Expected Token Savings: Prevents repeat confirmation loops; single model call per EXECUTING phase
# Environment: stateful session object; persist to Redis/DB for multi-process deployments
```

## Option 3: Hierarchical State Machine for Complex Flows

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class TopState(Enum):
    ONBOARDING = "onboarding"
    ACTIVE     = "active"
    SUPPORT    = "support"
    OFFBOARDED = "offboarded"

class OnboardingSubState(Enum):
    WELCOME        = "welcome"
    COLLECT_NAME   = "collect_name"
    COLLECT_GOAL   = "collect_goal"
    COMPLETE       = "complete"

class ActiveSubState(Enum):
    IDLE           = "idle"
    PROCESSING     = "processing"
    AWAITING_TOOL  = "awaiting_tool"

@dataclass
class HierarchicalSession:
    top: TopState = TopState.ONBOARDING
    sub: object = OnboardingSubState.WELCOME
    profile: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

def handle(session: HierarchicalSession, user_input: str) -> str:
    session.history.append({"role": "user", "content": user_input})

    reply = _dispatch(session, user_input)
    session.history.append({"role": "assistant", "content": reply})
    return reply

def _dispatch(session: HierarchicalSession, inp: str) -> str:
    if session.top == TopState.ONBOARDING:
        return _onboarding(session, inp)
    elif session.top == TopState.ACTIVE:
        return _active(session, inp)
    elif session.top == TopState.SUPPORT:
        return _support(session, inp)
    return "Session closed."

def _onboarding(session: HierarchicalSession, inp: str) -> str:
    sub = session.sub
    if sub == OnboardingSubState.WELCOME:
        session.sub = OnboardingSubState.COLLECT_NAME
        return "Welcome! What's your name?"

    elif sub == OnboardingSubState.COLLECT_NAME:
        session.profile["name"] = inp.strip()
        session.sub = OnboardingSubState.COLLECT_GOAL
        return f"Nice to meet you, {session.profile['name']}! What's your main goal today?"

    elif sub == OnboardingSubState.COLLECT_GOAL:
        session.profile["goal"] = inp.strip()
        session.sub = OnboardingSubState.COMPLETE
        # Transition to active state
        session.top = TopState.ACTIVE
        session.sub = ActiveSubState.IDLE
        return (f"Perfect, {session.profile['name']}! I'll help you with: "
                f"'{session.profile['goal']}'. What would you like to start with?")
    return "Onboarding error."

def _active(session: HierarchicalSession, inp: str) -> str:
    if inp.lower() in ["help", "support", "problem"]:
        session.top = TopState.SUPPORT
        return "Switching to support mode. What's the issue?"

    session.sub = ActiveSubState.PROCESSING
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"The user's name is {session.profile.get('name','User')}. "
               f"Their goal: {session.profile.get('goal','')}.",
        messages=session.history[-6:],  # last 3 turns
    )
    session.sub = ActiveSubState.IDLE
    return resp.content[0].text

def _support(session: HierarchicalSession, inp: str) -> str:
    if inp.lower() in ["resolved", "done", "thanks"]:
        session.top = TopState.ACTIVE
        session.sub = ActiveSubState.IDLE
        return "Glad it's resolved! Back to normal mode. What else can I help with?"
    return f"Support: I understand you're having trouble. Can you describe the issue further?"

session = HierarchicalSession()
for turn in ["", "Alice", "learn async Python", "explain generators", "help", "resolved", "show me an example"]:
    if not turn:
        print(f"Agent: {handle(session, '')}\n")
        continue
    print(f"User: {turn}")
    print(f"Agent [{session.top.value}/{session.sub.value if hasattr(session.sub,'value') else ''}]: {handle(session, turn)}\n")

# Expected Token Savings: Context injected only in ACTIVE state; onboarding uses zero model calls
# Environment: extend TopState/SubState for any multi-phase workflow (checkout, interview, etc.)
```

## Option 4: State Machine with SQLite Persistence

```python
import anthropic
import sqlite3
import json
import time
import uuid

client = anthropic.Anthropic()
DB = "conversation_fsm.db"

STATES = ["start", "topic_received", "outline_approved", "writing", "done"]

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_id TEXT PRIMARY KEY,
            state TEXT,
            data TEXT,
            updated_at REAL
        )
    """)
    con.commit(); con.close()

def load_session(session_id: str) -> dict:
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT state, data FROM conversations WHERE session_id=?", (session_id,)
    ).fetchone()
    con.close()
    if row:
        return {"state": row[0], "data": json.loads(row[1])}
    return {"state": "start", "data": {}}

def save_session(session_id: str, state: str, data: dict):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO conversations VALUES (?,?,?,?)",
        (session_id, state, json.dumps(data), time.time())
    )
    con.commit(); con.close()

def process_turn(session_id: str, user_input: str) -> str:
    session = load_session(session_id)
    state = session["state"]
    data = session["data"]

    if state == "start":
        data["topic"] = user_input.strip()
        new_state = "topic_received"
        # Generate outline
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Create a 3-point outline for: {data['topic']}"}],
        )
        data["outline"] = resp.content[0].text
        reply = f"Here's my outline:\n{data['outline']}\n\nApprove? (yes/edit)"

    elif state == "topic_received":
        if user_input.lower().startswith("yes"):
            new_state = "writing"
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": f"Write about: {data['topic']}\nOutline:\n{data['outline']}"}],
            )
            data["draft"] = resp.content[0].text
            new_state = "done"
            reply = f"Draft complete:\n\n{data['draft']}"
        else:
            data["outline"] = user_input
            reply = "Outline updated. Approve now? (yes)"

    elif state == "done":
        reply = "This conversation is complete. Start a new session for a new topic."
        new_state = "done"

    else:
        reply = "Unknown state. Please start a new session."
        new_state = "start"

    save_session(session_id, new_state, data)
    return reply

init_db()
sid = uuid.uuid4().hex[:8]
print(f"Session: {sid}\n")

for turn in ["The history of the internet", "yes"]:
    print(f"User: {turn}")
    print(f"Agent: {process_turn(sid, turn)[:200]}\n")

# Expected Token Savings: State persists across restarts; no need to replay history
# Environment: SQLite; swap for Redis/PostgreSQL in multi-process deployments
```

## Option 5: State Machine with Event-Driven Transitions

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

class AgentState(Enum):
    IDLE       = auto()
    LISTENING  = auto()
    THINKING   = auto()
    RESPONDING = auto()
    WAITING    = auto()
    ERROR      = auto()

Event = str  # e.g. "user_message", "model_done", "tool_done", "error"

@dataclass
class StateMachine:
    state: AgentState = AgentState.IDLE
    handlers: dict[tuple, Callable] = field(default_factory=dict)
    context: dict = field(default_factory=dict)

    def on(self, state: AgentState, event: Event):
        def decorator(fn: Callable[..., Awaitable]):
            self.handlers[(state, event)] = fn
            return fn
        return decorator

    async def emit(self, event: Event, **kwargs) -> str | None:
        key = (self.state, event)
        handler = self.handlers.get(key)
        if not handler:
            print(f"  [FSM] No handler for ({self.state.name}, {event})")
            return None
        return await handler(self, **kwargs)

sm = StateMachine()

@sm.on(AgentState.IDLE, "start")
async def on_start(machine: StateMachine, **_) -> str:
    machine.state = AgentState.LISTENING
    return "Ready. What would you like to discuss?"

@sm.on(AgentState.LISTENING, "user_message")
async def on_user_message(machine: StateMachine, message: str = "", **_) -> str:
    machine.context["last_input"] = message
    machine.state = AgentState.THINKING
    return await machine.emit("think")

@sm.on(AgentState.THINKING, "think")
async def on_think(machine: StateMachine, **_) -> str:
    machine.state = AgentState.RESPONDING
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": machine.context["last_input"]}],
        )
        machine.context["last_output"] = resp.content[0].text
        machine.state = AgentState.WAITING
        return machine.context["last_output"]
    except Exception as e:
        machine.state = AgentState.ERROR
        await machine.emit("error", error=str(e))
        return "An error occurred."

@sm.on(AgentState.WAITING, "user_message")
async def on_followup(machine: StateMachine, message: str = "", **_) -> str:
    if message.lower() in ["quit", "exit", "done"]:
        machine.state = AgentState.IDLE
        return "Goodbye!"
    machine.context["last_input"] = message
    machine.state = AgentState.THINKING
    return await machine.emit("think")

@sm.on(AgentState.ERROR, "error")
async def on_error(machine: StateMachine, error: str = "", **_) -> str:
    print(f"  [ERROR] {error}")
    machine.state = AgentState.LISTENING
    return f"Error: {error}. Please try again."

async def main():
    print(f"Agent: {await sm.emit('start')}")
    for turn in ["What is recursion?", "Can you give an example?", "done"]:
        print(f"User: {turn}")
        reply = await sm.emit("user_message", message=turn)
        print(f"Agent [{sm.state.name}]: {reply}\n")

asyncio.run(main())

# Expected Token Savings: No wasted calls in IDLE/ERROR states; transitions gate model access
# Environment: async; event-driven handlers are easy to test in isolation
```

## Option 6: State Machine with Visualization and Audit Trail

```python
import anthropic
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()
DB = "fsm_audit.db"

class S(Enum):
    START    = "START"
    INTENT   = "INTENT"
    PARAMS   = "PARAMS"
    EXECUTE  = "EXECUTE"
    VERIFY   = "VERIFY"
    COMPLETE = "COMPLETE"
    FAIL     = "FAIL"

GRAPH: dict[S, dict[str, S]] = {
    S.START:    {"provide_input": S.INTENT},
    S.INTENT:   {"clarify": S.PARAMS, "execute_direct": S.EXECUTE},
    S.PARAMS:   {"params_ready": S.EXECUTE},
    S.EXECUTE:  {"success": S.VERIFY, "failure": S.FAIL},
    S.VERIFY:   {"approved": S.COMPLETE, "rejected": S.EXECUTE},
    S.FAIL:     {"retry": S.INTENT, "abort": S.COMPLETE},
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS transitions (
            session_id TEXT, from_state TEXT, event TEXT,
            to_state TEXT, ts REAL
        )
    """)
    con.commit(); con.close()

@dataclass
class AuditedFSM:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: S = S.START
    data: dict = field(default_factory=dict)

    def transition(self, event: str) -> S:
        targets = GRAPH.get(self.state, {})
        if event not in targets:
            raise ValueError(f"Event '{event}' invalid in state {self.state.value}")
        new_state = targets[event]
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO transitions VALUES (?,?,?,?,?)",
                    (self.session_id, self.state.value, event, new_state.value, time.time()))
        con.commit(); con.close()
        print(f"  [FSM] {self.state.value} --{event}--> {new_state.value}")
        self.state = new_state
        return new_state

    def audit_trail(self) -> list[dict]:
        con = sqlite3.connect(DB)
        rows = con.execute(
            "SELECT from_state, event, to_state, ts FROM transitions WHERE session_id=? ORDER BY ts",
            (self.session_id,)
        ).fetchall()
        con.close()
        return [{"from": r[0], "event": r[1], "to": r[2], "ts": r[3]} for r in rows]

def run_session(fsm: AuditedFSM, task: str) -> str:
    fsm.data["task"] = task
    fsm.transition("provide_input")

    # Classify intent: does it need params?
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content":
            f"Does this task need clarification? Answer 'yes' or 'no': {task}"}],
    )
    needs_clarification = "yes" in resp.content[0].text.lower()

    if needs_clarification:
        fsm.transition("clarify")
        fsm.data["params"] = "detailed output, formal tone"
        fsm.transition("params_ready")
    else:
        fsm.transition("execute_direct")

    # Execute
    exec_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"{task}\n\nParams: {fsm.data.get('params', 'default')}"}],
    )
    fsm.data["result"] = exec_resp.content[0].text
    fsm.transition("success")

    # Auto-approve for demo
    fsm.transition("approved")
    return fsm.data["result"]

init_db()
fsm = AuditedFSM()
result = run_session(fsm, "Write a haiku about distributed systems.")
print(f"\nResult:\n{result}")

print(f"\nAudit trail for session {fsm.session_id}:")
for t in fsm.audit_trail():
    print(f"  {t['from']:10s} --{t['event']:18s}--> {t['to']}")

# Expected Token Savings: Two targeted Haiku calls (classify + execute); no drift
# Environment: SQLite audit; swap print/DB with your observability stack
```

## Comparison

| Option | Architecture | Persistence | Best For |
|--------|-------------|-------------|----------|
| 1 — Enum + Guards | Simple enum FSM with guard checks | In-memory | Linear single-session flows |
| 2 — Timeout + Reprompt | Timed phases with retry limits | In-memory | User-facing flows needing idle detection |
| 3 — Hierarchical | Nested top/sub states | In-memory | Complex branching (onboarding + active + support) |
| 4 — SQLite Persistent | State stored per session_id | SQLite | Multi-turn across requests/restarts |
| 5 — Event-Driven | Event/handler dispatch table | In-memory | Async servers; easily unit-testable handlers |
| 6 — Audited FSM | Explicit graph + transition log | SQLite audit | Compliance, debugging, visualization |
