---
layout: solution
title: "Agent Doesn't Implement Conversation State Machine"
category: general
description: "Agents that handle all user messages with the same logic regardless of conversation phase produce incoherent multi-turn interactions. A conversation state machine defines explicit states, valid transitions, and per-state prompts — making agent behavior predictable and debuggable."
tags: [general, state-machine, conversation, multi-turn, workflow, python]
---

## Problem

Without a state machine, agents respond to each message in isolation. A customer support agent might answer billing questions when it should be in an authentication state, or provide detailed help when the user hasn't agreed to terms. State machines enforce conversation flow: each state has a defined purpose, accepted transitions, and a specific prompt configuration — making the conversation reliable, testable, and auditable.

## Solutions

### Option 1: Simple Enum State Machine with Transition Table

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class State(Enum):
    GREETING = "greeting"
    COLLECTING_INTENT = "collecting_intent"
    ANSWERING = "answering"
    CLARIFYING = "clarifying"
    CLOSING = "closing"
    DONE = "done"

# Transition table: (current_state, user_signal) → new_state
TRANSITIONS: dict[tuple[State, str], State] = {
    (State.GREETING, "intent_provided"):         State.ANSWERING,
    (State.GREETING, "unclear"):                 State.COLLECTING_INTENT,
    (State.COLLECTING_INTENT, "intent_clear"):   State.ANSWERING,
    (State.COLLECTING_INTENT, "still_unclear"):  State.CLARIFYING,
    (State.ANSWERING, "follow_up"):              State.ANSWERING,
    (State.ANSWERING, "needs_clarification"):    State.CLARIFYING,
    (State.ANSWERING, "satisfied"):              State.CLOSING,
    (State.CLARIFYING, "clarified"):             State.ANSWERING,
    (State.CLOSING, "acknowledged"):             State.DONE,
}

STATE_PROMPTS = {
    State.GREETING: "You are starting a conversation. Greet the user warmly and ask how you can help. Keep it brief.",
    State.COLLECTING_INTENT: "The user's request is unclear. Politely ask clarifying questions to understand what they need.",
    State.ANSWERING: "Answer the user's question helpfully and completely. Offer to clarify if needed.",
    State.CLARIFYING: "The user needs clarification. Address their confusion specifically and clearly.",
    State.CLOSING: "The conversation is wrapping up. Thank the user and offer any final help.",
    State.DONE: "The conversation is complete.",
}

def detect_signal(client: anthropic.Anthropic, user_message: str,
                  current_state: State) -> str:
    """Use a lightweight call to classify the user's message as a transition signal."""
    valid_signals = {s for (st, s) in TRANSITIONS if st == current_state}
    if not valid_signals:
        return "done"
    prompt = (f"Classify this message into one of: {sorted(valid_signals)}\n"
              f"Message: \"{user_message}\"\nReply with just the signal name.")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    detected = r.content[0].text.strip().lower().replace(" ", "_")
    return detected if detected in valid_signals else list(valid_signals)[0]

@dataclass
class ConversationFSM:
    state: State = State.GREETING
    history: list[dict] = field(default_factory=list)
    turn_count: int = 0

    def transition(self, signal: str) -> None:
        key = (self.state, signal)
        if key in TRANSITIONS:
            old = self.state
            self.state = TRANSITIONS[key]
            print(f"  [FSM] {old.value} --{signal}--> {self.state.value}")
        else:
            print(f"  [FSM] No transition from {self.state.value} on '{signal}'")

def run_conversation():
    client = anthropic.Anthropic()
    fsm = ConversationFSM()

    print(f"[Initial State: {fsm.state.value}]")

    # Simulate a conversation
    user_messages = [
        "",                     # Trigger greeting
        "I need help with Python decorators.",
        "Can you show me an example?",
        "That makes sense, thank you!",
        "Goodbye!",
    ]

    for user_msg in user_messages:
        if fsm.state == State.DONE:
            break
        fsm.turn_count += 1
        print(f"\n[Turn {fsm.turn_count} | State: {fsm.state.value}]")

        if user_msg:
            print(f"User: {user_msg}")
            fsm.history.append({"role": "user", "content": user_msg})
            signal = detect_signal(client, user_msg, fsm.state)
            fsm.transition(signal)

        # Generate response for current state
        system = STATE_PROMPTS[fsm.state]
        messages = fsm.history if fsm.history else [{"role": "user", "content": "Hello"}]
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=system,
            messages=messages,
        )
        agent_text = response.content[0].text
        fsm.history.append({"role": "assistant", "content": agent_text})
        print(f"Agent: {agent_text[:80]}")

if __name__ == "__main__":
    run_conversation()

# Expected Token Savings: State-specific prompts are shorter than a single mega-prompt
# Environment: pip install anthropic
```

### Option 2: Typed State Machine with Entry/Exit Hooks

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable
import asyncio

class OrderState(Enum):
    IDLE = "idle"
    AUTHENTICATING = "authenticating"
    BROWSING = "browsing"
    ORDERING = "ordering"
    CONFIRMING = "confirming"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

@dataclass
class StateContext:
    user_id: Optional[str] = None
    cart: list[str] = field(default_factory=list)
    order_id: Optional[str] = None
    auth_attempts: int = 0
    history: list[dict] = field(default_factory=list)

@dataclass
class StateNode:
    state: OrderState
    system_prompt: str
    on_enter: Optional[Callable[["OrderFSM"], None]] = None
    on_exit: Optional[Callable[["OrderFSM"], None]] = None
    max_turns: int = 5

class OrderFSM:
    def __init__(self, client: anthropic.AsyncAnthropic):
        self.client = client
        self.ctx = StateContext()
        self._turn = 0

        self._nodes: dict[OrderState, StateNode] = {
            OrderState.IDLE: StateNode(
                OrderState.IDLE,
                "Welcome! Ask the user to log in to continue.",
                on_enter=lambda fsm: print("[Enter IDLE] Awaiting authentication"),
            ),
            OrderState.AUTHENTICATING: StateNode(
                OrderState.AUTHENTICATING,
                "Ask the user for their account ID. Accept any 3-digit ID as valid.",
                on_enter=lambda fsm: print("[Enter AUTH] Requesting credentials"),
                on_exit=lambda fsm: print(f"[Exit AUTH] Authenticated as {fsm.ctx.user_id}"),
                max_turns=3,
            ),
            OrderState.BROWSING: StateNode(
                OrderState.BROWSING,
                "Help the user browse products. Suggest items based on their request.",
                on_enter=lambda fsm: print(f"[Enter BROWSE] Cart: {fsm.ctx.cart}"),
            ),
            OrderState.ORDERING: StateNode(
                OrderState.ORDERING,
                "Process the user's order. Ask for quantity and confirm items.",
                on_enter=lambda fsm: print(f"[Enter ORDER] Processing: {fsm.ctx.cart}"),
            ),
            OrderState.CONFIRMING: StateNode(
                OrderState.CONFIRMING,
                "Show order summary and ask user to confirm or cancel.",
                on_enter=lambda fsm: print("[Enter CONFIRM] Showing summary"),
            ),
            OrderState.COMPLETED: StateNode(
                OrderState.COMPLETED,
                "Order placed. Provide confirmation and thank the user.",
                on_enter=lambda fsm: print(f"[Enter COMPLETE] Order #{fsm.ctx.order_id}"),
            ),
            OrderState.CANCELLED: StateNode(
                OrderState.CANCELLED,
                "Order cancelled. Offer to help with something else.",
            ),
        }
        self._state = OrderState.IDLE
        self._enter_state(self._state)

    def _enter_state(self, state: OrderState) -> None:
        node = self._nodes.get(state)
        if node and node.on_enter:
            node.on_enter(self)

    def _exit_state(self, state: OrderState) -> None:
        node = self._nodes.get(state)
        if node and node.on_exit:
            node.on_exit(self)

    def transition_to(self, new_state: OrderState) -> None:
        self._exit_state(self._state)
        old = self._state
        self._state = new_state
        self._enter_state(self._state)
        print(f"  → {old.value} → {new_state.value}")

    async def respond(self, user_message: str) -> str:
        self.ctx.history.append({"role": "user", "content": user_message})
        node = self._nodes[self._state]

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=node.system_prompt,
            messages=self.ctx.history[-6:],  # Last 6 turns
        )
        text = response.content[0].text
        self.ctx.history.append({"role": "assistant", "content": text})
        self._turn += 1

        # Simple rule-based transitions (in production: LLM classifier)
        msg_lower = user_message.lower()
        if self._state == OrderState.IDLE:
            self.transition_to(OrderState.AUTHENTICATING)
        elif self._state == OrderState.AUTHENTICATING:
            import re
            if re.search(r'\b\d{3}\b', user_message):
                self.ctx.user_id = re.search(r'\b(\d{3})\b', user_message).group(1)
                self.transition_to(OrderState.BROWSING)
        elif self._state == OrderState.BROWSING and any(
            w in msg_lower for w in ["add", "order", "buy", "want"]
        ):
            self.ctx.cart.append(user_message.split()[-1])
            self.transition_to(OrderState.ORDERING)
        elif self._state == OrderState.ORDERING and "confirm" in msg_lower:
            self.ctx.order_id = "ORD-42"
            self.transition_to(OrderState.CONFIRMING)
        elif self._state == OrderState.CONFIRMING:
            if "yes" in msg_lower or "confirm" in msg_lower:
                self.transition_to(OrderState.COMPLETED)
            elif "cancel" in msg_lower:
                self.transition_to(OrderState.CANCELLED)

        return text

async def main():
    client = anthropic.AsyncAnthropic()
    fsm = OrderFSM(client)

    script = [
        "Hello, I need help.",
        "My ID is 123.",
        "I want to buy a laptop.",
        "Please confirm my order.",
        "Yes, confirm it.",
    ]
    for msg in script:
        if fsm._state in (OrderState.COMPLETED, OrderState.CANCELLED):
            break
        print(f"\nUser: {msg}")
        response = await fsm.respond(msg)
        print(f"Agent [{fsm._state.value}]: {response[:70]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Targeted per-state prompts avoid bloated system prompts
# Environment: pip install anthropic
```

### Option 3: Hierarchical State Machine with Sub-States

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class TopState(Enum):
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    SUPPORT = "support"
    OFFBOARDING = "offboarding"

class OnboardingSubstate(Enum):
    WELCOME = "welcome"
    COLLECT_NAME = "collect_name"
    COLLECT_GOAL = "collect_goal"
    CONFIRM_SETUP = "confirm_setup"

class ActiveSubstate(Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    REVIEWING = "reviewing"

@dataclass
class HierarchicalState:
    top: TopState = TopState.ONBOARDING
    sub: Optional[Enum] = None
    depth: int = 0

    def __post_init__(self):
        self.sub = OnboardingSubstate.WELCOME

    @property
    def full_name(self) -> str:
        return f"{self.top.value}.{self.sub.value if self.sub else 'root'}"

@dataclass
class UserProfile:
    name: Optional[str] = None
    goal: Optional[str] = None
    completed_onboarding: bool = False
    turn_count: int = 0

SYSTEM_PROMPTS = {
    "onboarding.welcome":       "Welcome the user warmly. Tell them you'll ask a few setup questions.",
    "onboarding.collect_name":  "Ask the user for their preferred name. Be friendly.",
    "onboarding.collect_goal":  "Ask what the user's primary goal is for using this assistant.",
    "onboarding.confirm_setup": "Confirm the user's name and goal. Ask if they're ready to begin.",
    "active.idle":              "You are a helpful assistant. The user is set up and ready.",
    "active.processing":        "You are actively helping with the user's task. Be focused.",
    "active.reviewing":         "Review your previous response with the user. Offer refinements.",
    "support.root":             "Provide customer support. Be empathetic and solution-focused.",
    "offboarding.root":         "Thank the user for using the service. Offer any parting help.",
}

class HierarchicalFSM:
    def __init__(self):
        self.state = HierarchicalState()
        self.profile = UserProfile()
        self.history: list[dict] = []

    def system_prompt(self) -> str:
        return SYSTEM_PROMPTS.get(self.state.full_name,
                                   SYSTEM_PROMPTS.get(f"{self.state.top.value}.root",
                                                       "Be helpful."))

    def advance(self, user_message: str) -> None:
        msg = user_message.lower()
        st, sub = self.state.top, self.state.sub

        if st == TopState.ONBOARDING:
            if sub == OnboardingSubstate.WELCOME:
                self.state.sub = OnboardingSubstate.COLLECT_NAME
            elif sub == OnboardingSubstate.COLLECT_NAME and len(user_message) > 1:
                self.profile.name = user_message.strip().split()[0].capitalize()
                self.state.sub = OnboardingSubstate.COLLECT_GOAL
            elif sub == OnboardingSubstate.COLLECT_GOAL and len(user_message) > 5:
                self.profile.goal = user_message
                self.state.sub = OnboardingSubstate.CONFIRM_SETUP
            elif sub == OnboardingSubstate.CONFIRM_SETUP and ("yes" in msg or "ready" in msg):
                self.profile.completed_onboarding = True
                self.state.top = TopState.ACTIVE
                self.state.sub = ActiveSubstate.IDLE

        elif st == TopState.ACTIVE:
            if "help" in msg or "support" in msg:
                self.state.top = TopState.SUPPORT
                self.state.sub = None
            elif "bye" in msg or "done" in msg:
                self.state.top = TopState.OFFBOARDING
                self.state.sub = None
            elif sub == ActiveSubstate.IDLE and len(user_message) > 10:
                self.state.sub = ActiveSubstate.PROCESSING
            elif sub == ActiveSubstate.PROCESSING:
                self.state.sub = ActiveSubstate.IDLE

        print(f"  [FSM] → {self.state.full_name}")

def run_hierarchical():
    client = anthropic.Anthropic()
    fsm = HierarchicalFSM()

    script = [
        "Hello!",
        "Alice",
        "I want to learn Python programming.",
        "Yes, I'm ready!",
        "Can you explain list comprehensions?",
        "That was great, bye!",
    ]

    for msg in script:
        fsm.profile.turn_count += 1
        print(f"\nUser: {msg}")
        fsm.advance(msg)
        fsm.history.append({"role": "user", "content": msg})

        system = fsm.system_prompt()
        if fsm.profile.name:
            system += f" The user's name is {fsm.profile.name}."

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=system,
            messages=fsm.history[-4:],
        )
        text = response.content[0].text
        fsm.history.append({"role": "assistant", "content": text})
        print(f"Agent [{fsm.state.full_name}]: {text[:70]}")

if __name__ == "__main__":
    run_hierarchical()

# Expected Token Savings: Hierarchical states use focused prompts; deeper states can use smaller models
# Environment: pip install anthropic
```

### Option 4: Event-Driven State Machine with Guards

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

class TechSupportState(Enum):
    INTAKE = "intake"
    DIAGNOSIS = "diagnosis"
    SOLUTION = "solution"
    ESCALATION = "escalation"
    RESOLUTION = "resolution"
    CLOSED = "closed"

@dataclass
class Event:
    name: str
    payload: dict = field(default_factory=dict)

@dataclass
class Transition:
    from_state: TechSupportState
    event: str
    to_state: TechSupportState
    guard: Optional[Callable[[dict], bool]] = None  # Must return True to allow
    action: Optional[Callable[[dict], None]] = None

@dataclass
class SupportContext:
    ticket_id: str = "TKT-0001"
    issue_description: str = ""
    severity: int = 2          # 1=critical, 2=high, 3=medium, 4=low
    resolution_steps: list[str] = field(default_factory=list)
    escalated: bool = False
    resolved: bool = False
    turn_count: int = 0

class EventDrivenFSM:
    def __init__(self, ctx: SupportContext):
        self._state = TechSupportState.INTAKE
        self._ctx = ctx
        self._transitions: list[Transition] = [
            Transition(TechSupportState.INTAKE, "issue_described",
                       TechSupportState.DIAGNOSIS),
            Transition(TechSupportState.DIAGNOSIS, "solution_found",
                       TechSupportState.SOLUTION,
                       guard=lambda c: c.get("severity", 2) <= 3),
            Transition(TechSupportState.DIAGNOSIS, "needs_escalation",
                       TechSupportState.ESCALATION,
                       guard=lambda c: c.get("severity", 2) == 1,
                       action=lambda c: c.update({"escalated": True})),
            Transition(TechSupportState.SOLUTION, "user_confirmed_resolved",
                       TechSupportState.RESOLUTION),
            Transition(TechSupportState.SOLUTION, "solution_failed",
                       TechSupportState.ESCALATION),
            Transition(TechSupportState.ESCALATION, "expert_responded",
                       TechSupportState.SOLUTION),
            Transition(TechSupportState.RESOLUTION, "ticket_closed",
                       TechSupportState.CLOSED,
                       action=lambda c: c.update({"resolved": True})),
        ]

    def fire(self, event: Event) -> bool:
        ctx_dict = {
            "severity": self._ctx.severity,
            "escalated": self._ctx.escalated,
            "resolved": self._ctx.resolved,
        }
        for t in self._transitions:
            if t.from_state == self._state and t.event == event.name:
                if t.guard and not t.guard(ctx_dict):
                    print(f"  [GUARD] Transition blocked by guard condition")
                    continue
                if t.action:
                    t.action(ctx_dict)
                    self._ctx.escalated = ctx_dict.get("escalated", False)
                    self._ctx.resolved = ctx_dict.get("resolved", False)
                old = self._state
                self._state = t.to_state
                print(f"  [EVENT] {event.name}: {old.value} → {self._state.value}")
                return True
        print(f"  [EVENT] {event.name}: no valid transition from {self._state.value}")
        return False

STATE_CONFIGS = {
    TechSupportState.INTAKE:
        "You are starting a tech support session. Acknowledge the issue and ask for details.",
    TechSupportState.DIAGNOSIS:
        "Diagnose the technical issue. Ask probing questions to understand root cause.",
    TechSupportState.SOLUTION:
        "Provide a clear step-by-step solution. Check if the user can follow along.",
    TechSupportState.ESCALATION:
        "This issue requires escalation. Explain what will happen next and set expectations.",
    TechSupportState.RESOLUTION:
        "Confirm the issue is resolved. Ask if there's anything else to address.",
    TechSupportState.CLOSED:
        "Close the ticket. Thank the user and provide ticket reference.",
}

async def run_support_session():
    client = anthropic.AsyncAnthropic()
    ctx = SupportContext(ticket_id="TKT-4242", severity=2)
    fsm = EventDrivenFSM(ctx)
    history: list[dict] = []

    interactions = [
        ("My application keeps crashing on startup after the latest update.", "issue_described"),
        ("No error logs, it just closes immediately.", "needs_escalation"),
        ("Please restart your app in safe mode and check settings.", "expert_responded"),
        ("It works now! Safe mode fixed it.", "user_confirmed_resolved"),
        ("No other issues, thanks!", "ticket_closed"),
    ]

    for user_msg, event_name in interactions:
        if fsm._state == TechSupportState.CLOSED:
            break
        print(f"\nUser [{fsm._state.value}]: {user_msg}")
        fsm.fire(Event(event_name))

        history.append({"role": "user", "content": user_msg})
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=STATE_CONFIGS[fsm._state],
            messages=history[-4:],
        )
        text = response.content[0].text
        history.append({"role": "assistant", "content": text})
        print(f"Agent [{fsm._state.value}]: {text[:70]}")

if __name__ == "__main__":
    asyncio.run(run_support_session())

# Expected Token Savings: Guards prevent invalid state transitions; no tokens wasted on wrong path
# Environment: pip install anthropic
```

### Option 5: Persistent State Machine with SQLite Checkpoint

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Generator

class SalesState(Enum):
    PROSPECTING = "prospecting"
    QUALIFYING = "qualifying"
    PITCHING = "pitching"
    HANDLING_OBJECTIONS = "handling_objections"
    CLOSING = "closing"
    WON = "won"
    LOST = "lost"

SALES_PROMPTS = {
    SalesState.PROSPECTING:          "Introduce your product briefly. Ask open-ended discovery questions.",
    SalesState.QUALIFYING:           "Assess the prospect's needs, budget, and timeline. Be consultative.",
    SalesState.PITCHING:             "Present your solution tailored to their specific needs.",
    SalesState.HANDLING_OBJECTIONS:  "Address the prospect's concerns with empathy and evidence.",
    SalesState.CLOSING:              "Make a clear call-to-action. Ask for the sale directly.",
    SalesState.WON:                  "Congratulate the customer and outline next steps.",
    SalesState.LOST:                 "Thank the prospect for their time. Leave the door open.",
}

class PersistentSalesFSM:
    def __init__(self, session_id: str, db_path: str = "/tmp/sales_fsm.db"):
        self.session_id = session_id
        self._db_path = db_path
        self._init_db()
        self._load_or_create()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS fsm_sessions (
                session_id TEXT PRIMARY KEY,
                current_state TEXT NOT NULL,
                history_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL,
                updated_at REAL
            )""")

    def _load_or_create(self) -> None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM fsm_sessions WHERE session_id=?",
                               (self.session_id,)).fetchone()
        if row:
            self.state = SalesState(row["current_state"])
            self.history = json.loads(row["history_json"])
            self.metadata = json.loads(row["metadata_json"])
            print(f"[FSM] Restored session {self.session_id}: state={self.state.value}")
        else:
            self.state = SalesState.PROSPECTING
            self.history = []
            self.metadata = {"turn_count": 0, "objections_handled": 0}
            self._save()

    def _save(self) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO fsm_sessions
                VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM fsm_sessions WHERE session_id=?), ?), ?)""",
                (self.session_id, self.state.value,
                 json.dumps(self.history), json.dumps(self.metadata),
                 self.session_id, time.time(), time.time()))

    def transition_to(self, new_state: SalesState) -> None:
        old = self.state
        self.state = new_state
        self._save()
        print(f"  [FSM] {old.value} → {new_state.value} (persisted)")

    def process_turn(self, client: anthropic.Anthropic, user_message: str) -> str:
        self.metadata["turn_count"] += 1
        self.history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=SALES_PROMPTS[self.state],
            messages=self.history[-6:],
        )
        text = response.content[0].text
        self.history.append({"role": "assistant", "content": text})

        # Auto-advance state based on keywords
        msg_lower = user_message.lower()
        if self.state == SalesState.PROSPECTING and any(
            w in msg_lower for w in ["interested", "tell me more", "how much"]
        ):
            self.transition_to(SalesState.QUALIFYING)
        elif self.state == SalesState.QUALIFYING and "budget" in msg_lower:
            self.transition_to(SalesState.PITCHING)
        elif self.state == SalesState.PITCHING and "concern" in msg_lower:
            self.transition_to(SalesState.HANDLING_OBJECTIONS)
        elif self.state == SalesState.PITCHING and "yes" in msg_lower:
            self.transition_to(SalesState.CLOSING)
        elif self.state == SalesState.CLOSING and "deal" in msg_lower:
            self.transition_to(SalesState.WON)

        self._save()
        return text

if __name__ == "__main__":
    client = anthropic.Anthropic()
    session_id = "sales-" + str(uuid.uuid4())[:8]
    fsm = PersistentSalesFSM(session_id)

    conv = [
        "Hi, I heard about your product.",
        "Tell me more, I'm interested.",
        "What's the budget range?",
        "I have a concern about integration.",
        "Actually yes, let's do the deal.",
    ]
    for msg in conv:
        print(f"\nUser: {msg}")
        response = fsm.process_turn(client, msg)
        print(f"Agent [{fsm.state.value}]: {response[:70]}")

    print(f"\nFinal state: {fsm.state.value} | Turns: {fsm.metadata['turn_count']}")

# Expected Token Savings: Persistent state allows resuming conversations; shorter context per turn
# Environment: pip install anthropic; sqlite3 is stdlib
```

### Option 6: Async State Machine with Timeout and Fallback

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class InterviewState(Enum):
    INTRO = "intro"
    TECHNICAL_Q1 = "technical_q1"
    TECHNICAL_Q2 = "technical_q2"
    BEHAVIORAL = "behavioral"
    CANDIDATE_QUESTIONS = "candidate_questions"
    WRAP_UP = "wrap_up"
    TIMEOUT = "timeout"
    COMPLETE = "complete"

STATE_SEQUENCE = [
    InterviewState.INTRO,
    InterviewState.TECHNICAL_Q1,
    InterviewState.TECHNICAL_Q2,
    InterviewState.BEHAVIORAL,
    InterviewState.CANDIDATE_QUESTIONS,
    InterviewState.WRAP_UP,
    InterviewState.COMPLETE,
]

STATE_PROMPTS = {
    InterviewState.INTRO:               "Start the interview. Introduce yourself briefly and warm up the candidate.",
    InterviewState.TECHNICAL_Q1:        "Ask a Python technical question about data structures. Evaluate the answer.",
    InterviewState.TECHNICAL_Q2:        "Ask a system design question. Focus on scalability thinking.",
    InterviewState.BEHAVIORAL:          "Ask about a time they dealt with conflict on a team. Use STAR format.",
    InterviewState.CANDIDATE_QUESTIONS: "Invite the candidate to ask questions about the role and company.",
    InterviewState.WRAP_UP:             "Thank the candidate. Explain next steps in the hiring process.",
    InterviewState.TIMEOUT:             "The time limit for this section has been reached. Move to the next question.",
    InterviewState.COMPLETE:            "The interview is complete. Wish the candidate well.",
}

STATE_TIME_LIMITS = {
    InterviewState.INTRO: 60,
    InterviewState.TECHNICAL_Q1: 300,
    InterviewState.TECHNICAL_Q2: 300,
    InterviewState.BEHAVIORAL: 180,
    InterviewState.CANDIDATE_QUESTIONS: 120,
    InterviewState.WRAP_UP: 60,
}

@dataclass
class InterviewSession:
    state: InterviewState = InterviewState.INTRO
    state_entered_at: float = field(default_factory=time.monotonic)
    history: list[dict] = field(default_factory=list)
    scores: dict[str, Optional[int]] = field(default_factory=dict)

    def time_in_state(self) -> float:
        return time.monotonic() - self.state_entered_at

    def is_timed_out(self) -> bool:
        limit = STATE_TIME_LIMITS.get(self.state)
        return limit is not None and self.time_in_state() > limit

    def advance(self) -> None:
        try:
            idx = STATE_SEQUENCE.index(self.state)
            if idx + 1 < len(STATE_SEQUENCE):
                self.state = STATE_SEQUENCE[idx + 1]
                self.state_entered_at = time.monotonic()
                print(f"  [FSM] → {self.state.value}")
        except ValueError:
            pass

async def run_interview():
    client = anthropic.AsyncAnthropic()
    session = InterviewSession()
    # Reduce time limits for demo
    for s in STATE_TIME_LIMITS:
        STATE_TIME_LIMITS[s] = 5  # 5 seconds per state for demo

    candidate_responses = [
        "Hello, I'm excited to be here!",
        "I'd use a hash map for O(1) lookups.",
        "I'd use horizontal scaling and caching.",
        "I mediated by focusing on shared goals.",
        "What's the tech stack and team size?",
        "Thank you, this was great!",
    ]

    for candidate_reply in candidate_responses:
        if session.state == InterviewState.COMPLETE:
            break

        print(f"\n[State: {session.state.value} | {session.time_in_state():.1f}s]")

        # Check timeout
        if session.is_timed_out():
            print(f"  [TIMEOUT] State {session.state.value} exceeded time limit")
            # Generate timeout notice then advance
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=50,
                system=STATE_PROMPTS[InterviewState.TIMEOUT],
                messages=[{"role": "user", "content": "timeout"}],
            )
            print(f"Agent [TIMEOUT]: {r.content[0].text[:60]}")
            session.advance()

        print(f"Candidate: {candidate_reply}")
        session.history.append({"role": "user", "content": candidate_reply})

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=STATE_PROMPTS[session.state],
            messages=session.history[-4:],
        )
        text = response.content[0].text
        session.history.append({"role": "assistant", "content": text})
        print(f"Interviewer [{session.state.value}]: {text[:70]}")

        session.advance()
        await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(run_interview())

# Expected Token Savings: Time-bounded states prevent runaway conversations that consume excess tokens
# Environment: pip install anthropic
```

## Comparison

| Option | State Storage | Transitions | Guards | Persistence | Best For |
|--------|--------------|------------|--------|-------------|----------|
| 1. Enum + table | In-memory | Lookup table | None | No | Simple linear flows |
| 2. Typed nodes + hooks | In-memory | Rule-based | None | No | Ordered workflows |
| 3. Hierarchical | In-memory | Rule-based | None | No | Nested contexts |
| 4. Event-driven | In-memory | Event bus | Yes | No | Complex business logic |
| 5. Persistent SQLite | SQLite | Rule-based | None | Yes | Long-running sessions |
| 6. Async + timeout | In-memory | Sequential | Timeout | No | Time-bounded interviews |
