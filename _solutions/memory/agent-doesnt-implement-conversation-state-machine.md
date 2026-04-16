---
title: "Agent Doesn't Implement Conversation State Machine"
slug: agent-doesnt-implement-conversation-state-machine
category: memory
tags: [state-machine, conversation, flow, memory, context, anthropic-sdk]
description: >
  The agent handles every user turn with a single monolithic prompt, treating
  conversation as a flat list of messages. Without explicit state tracking,
  the agent cannot enforce multi-step flows (onboarding → confirm → execute),
  remember which phase a user is in after reconnection, or branch on user
  intent within a structured workflow.
symptoms:
  - Agent loses track of where it is in a multi-step workflow after a pause
  - User can skip required confirmation steps by phrasing requests cleverly
  - No way to resume an interrupted flow without restarting from scratch
  - System prompt bloat — all possible states crammed into one megaprompt
related_solutions:
  - agent-doesnt-implement-cost-per-conversation-tracking
  - agent-doesnt-implement-cooperative-cancellation-with-structured-concurrency
  - agent-doesnt-implement-hierarchical-memory-tiers
---

## Problem

Real conversations have structure: a purchase flow has cart → confirm → pay →
receipt; a support bot has triage → diagnose → resolve → close. Without an
explicit state machine the agent either (a) puts everything in the system
prompt and hopes the model tracks state, or (b) loses state across turns.
An explicit state machine makes transitions auditable, enforceable, and
resumable.

---

## Solution 1 — Enum-Based State Machine (Simplest)

Define conversation states as an `Enum`, transitions as a dict, and keep the
current state in a plain dataclass. The system prompt is generated from the
current state rather than hardcoded.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import Enum


class SupportState(Enum):
    TRIAGE    = "triage"
    DIAGNOSE  = "diagnose"
    RESOLVE   = "resolve"
    ESCALATE  = "escalate"
    CLOSED    = "closed"


TRANSITIONS: dict[SupportState, list[SupportState]] = {
    SupportState.TRIAGE:   [SupportState.DIAGNOSE, SupportState.ESCALATE],
    SupportState.DIAGNOSE: [SupportState.RESOLVE,  SupportState.ESCALATE],
    SupportState.RESOLVE:  [SupportState.CLOSED,   SupportState.DIAGNOSE],
    SupportState.ESCALATE: [SupportState.CLOSED],
    SupportState.CLOSED:   [],
}

STATE_PROMPTS: dict[SupportState, str] = {
    SupportState.TRIAGE:   "You are a support triage agent. Understand the user's issue category and severity. Ask 1-2 clarifying questions.",
    SupportState.DIAGNOSE: "You are diagnosing the issue. Ask targeted questions to identify root cause. When confident, propose a solution.",
    SupportState.RESOLVE:  "Present the solution clearly. Ask if it resolved the issue. If yes, close. If no, return to diagnose.",
    SupportState.ESCALATE: "Acknowledge you are escalating to a human agent. Collect contact details and issue summary.",
    SupportState.CLOSED:   "Thank the user and confirm the ticket is closed. Offer a satisfaction rating.",
}


@dataclass
class SupportConversation:
    conversation_id: str
    state: SupportState = SupportState.TRIAGE
    messages: list = field(default_factory=list)

    def can_transition(self, target: SupportState) -> bool:
        return target in TRANSITIONS[self.state]

    def transition(self, target: SupportState) -> None:
        if not self.can_transition(target):
            raise ValueError(f"Invalid transition: {self.state} -> {target}")
        print(f"[state] {self.state.value} -> {target.value}")
        self.state = target


async def support_turn(conv: SupportConversation, user_message: str) -> str:
    conv.messages.append({"role": "user", "content": user_message})
    client = anthropic.AsyncAnthropic()

    system = (
        f"{STATE_PROMPTS[conv.state]}\n\n"
        f"Current state: {conv.state.value}\n"
        f"Valid next states: {[s.value for s in TRANSITIONS[conv.state]]}\n"
        f"If you determine a state transition is needed, end your response with: [TRANSITION:<state>]"
    )

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=conv.messages,
    )
    text = resp.content[0].text

    # Parse optional transition directive
    import re
    m = re.search(r'\[TRANSITION:(\w+)\]', text)
    if m:
        try:
            target = SupportState(m.group(1))
            conv.transition(target)
        except (ValueError, KeyError):
            pass
        text = text[:m.start()].strip()

    conv.messages.append({"role": "assistant", "content": text})
    return text


async def demo_state_machine():
    conv = SupportConversation(conversation_id="ticket-001")

    turns = [
        "My application crashes when I upload large files.",
        "It's a Python Flask app. Crash happens on files > 50 MB.",
        "Yes that fixed it! The UPLOAD_MAX_CONTENT_LENGTH setting worked.",
    ]

    for user_msg in turns:
        print(f"\nUser [{conv.state.value}]: {user_msg}")
        reply = await support_turn(conv, user_msg)
        print(f"Agent: {reply[:120]}")

    print(f"\nFinal state: {conv.state.value}")


asyncio.run(demo_state_machine())
```

---

## Solution 2 — State Machine with Guard Conditions and Side Effects

Add guard conditions (must collect email before escalating) and side effects
(send webhook on close, create ticket on escalate) to transitions.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class ConvContext:
    user_email:    str | None = None
    issue_summary: str | None = None
    resolution:    str | None = None
    ticket_id:     str | None = None


@dataclass
class State:
    name:         str
    system_prompt: str
    transitions:  dict[str, "Transition"] = field(default_factory=dict)


@dataclass
class Transition:
    target:       str
    guard:        Callable[["ConvContext"], bool] | None = None
    side_effect:  Callable[["ConvContext"], Awaitable[None]] | None = None


# Side effects
async def create_ticket(ctx: ConvContext) -> None:
    import uuid
    ctx.ticket_id = f"TKT-{uuid.uuid4().hex[:6].upper()}"
    print(f"[side-effect] Ticket created: {ctx.ticket_id}")


async def send_close_webhook(ctx: ConvContext) -> None:
    print(f"[side-effect] Webhook: ticket {ctx.ticket_id} closed, resolution={ctx.resolution}")


STATES: dict[str, State] = {
    "triage": State(
        name="triage",
        system_prompt="Collect the user's name, email, and brief issue description.",
        transitions={
            "diagnose":  Transition("diagnose",  guard=lambda c: c.user_email is not None),
            "escalate":  Transition("escalate",  guard=lambda c: c.user_email is not None,
                                    side_effect=create_ticket),
        },
    ),
    "diagnose": State(
        name="diagnose",
        system_prompt="Diagnose and propose a fix. Ask if the fix resolved the issue.",
        transitions={
            "resolve":  Transition("resolve"),
            "escalate": Transition("escalate", side_effect=create_ticket),
        },
    ),
    "resolve": State(
        name="resolve",
        system_prompt="Confirm resolution and close the ticket.",
        transitions={
            "closed": Transition("closed", side_effect=send_close_webhook),
        },
    ),
    "escalate": State(
        name="escalate",
        system_prompt="Confirm escalation to a human agent.",
        transitions={
            "closed": Transition("closed", side_effect=send_close_webhook),
        },
    ),
    "closed": State(name="closed", system_prompt="Conversation closed."),
}


@dataclass
class StatefulConversation:
    conv_id:   str
    ctx:       ConvContext = field(default_factory=ConvContext)
    state_name: str = "triage"
    messages:  list = field(default_factory=list)

    @property
    def state(self) -> State:
        return STATES[self.state_name]

    async def transition(self, target_name: str) -> bool:
        t = self.state.transitions.get(target_name)
        if not t:
            return False
        if t.guard and not t.guard(self.ctx):
            print(f"[guard] transition to {target_name} blocked — guard condition not met")
            return False
        if t.side_effect:
            await t.side_effect(self.ctx)
        print(f"[state] {self.state_name} -> {target_name}")
        self.state_name = target_name
        return True


async def guarded_turn(conv: StatefulConversation, user_msg: str) -> str:
    import re
    conv.messages.append({"role": "user", "content": user_msg})

    # Extract email if present
    email_m = re.search(r'[\w.+-]+@[\w-]+\.\w+', user_msg)
    if email_m:
        conv.ctx.user_email = email_m.group()

    client = anthropic.AsyncAnthropic()
    system = (
        f"{conv.state.system_prompt}\n"
        f"State: {conv.state_name}  "
        f"Valid transitions: {list(conv.state.transitions.keys())}\n"
        f"Context: email={conv.ctx.user_email}\n"
        f"If transition needed, include [TRANSITION:<name>] at end."
    )
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        system=system, messages=conv.messages,
    )
    text = resp.content[0].text
    m = re.search(r'\[TRANSITION:(\w+)\]', text)
    if m:
        await conv.transition(m.group(1))
        text = text[:m.start()].strip()

    conv.messages.append({"role": "assistant", "content": text})
    return text


async def demo_guarded():
    conv = StatefulConversation("conv-guarded-001")
    for msg in [
        "Hi, I'm having trouble with login. My email is user@example.com",
        "Resetting my password worked! Thank you.",
    ]:
        print(f"\nUser [{conv.state_name}]: {msg}")
        reply = await guarded_turn(conv, msg)
        print(f"Agent: {reply[:100]}")


asyncio.run(demo_guarded())
```

---

## Solution 3 — Hierarchical State Machine (Parent + Child States)

Model complex workflows with parent states that contain substates. A "payment"
parent state has child states (enter_card, 3ds_auth, confirm). The agent
tracks both layers independently, enabling interrupt-and-resume.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field


@dataclass
class HSMState:
    name:       str
    parent:     str | None = None
    prompt:     str = ""
    children:   list[str] = field(default_factory=list)
    transitions: dict[str, str] = field(default_factory=dict)


HSM: dict[str, HSMState] = {
    "checkout": HSMState(
        "checkout", parent=None,
        prompt="Help the user complete their purchase.",
        children=["cart_review", "payment", "confirmation"],
        transitions={"abandon": "browsing"},
    ),
    "cart_review": HSMState(
        "cart_review", parent="checkout",
        prompt="Show cart contents and ask for confirmation to proceed to payment.",
        transitions={"proceed": "payment", "modify": "cart_review"},
    ),
    "payment": HSMState(
        "payment", parent="checkout",
        prompt="Collect payment details.",
        children=["enter_card", "3ds_auth"],
        transitions={"cancel": "cart_review", "authorize": "confirmation"},
    ),
    "enter_card": HSMState(
        "enter_card", parent="payment",
        prompt="Ask for card number, expiry, CVV.",
        transitions={"submitted": "3ds_auth"},
    ),
    "3ds_auth": HSMState(
        "3ds_auth", parent="payment",
        prompt="Prompt user to complete 3DS authentication in their banking app.",
        transitions={"authenticated": "confirmation", "failed": "enter_card"},
    ),
    "confirmation": HSMState(
        "confirmation", parent="checkout",
        prompt="Confirm the order and provide order number.",
        transitions={"done": "complete"},
    ),
    "complete": HSMState("complete", parent=None, prompt="Order placed. Offer help."),
    "browsing": HSMState("browsing", parent=None, prompt="Help the user browse products."),
}


@dataclass
class HSMConversation:
    conv_id:  str
    state:    str = "cart_review"
    messages: list = field(default_factory=list)

    def ancestors(self) -> list[str]:
        chain = []
        node  = HSM.get(self.state)
        while node and node.parent:
            chain.append(node.parent)
            node = HSM.get(node.parent)
        return list(reversed(chain))

    def full_prompt(self) -> str:
        """Compose prompt from parent states + current state."""
        chain = self.ancestors() + [self.state]
        parts = [HSM[s].prompt for s in chain if s in HSM]
        return " ".join(parts)

    def transition(self, event: str) -> bool:
        node = HSM.get(self.state)
        if not node:
            return False
        if event in node.transitions:
            self.state = node.transitions[event]
            return True
        # Check parent transitions
        parent_name = node.parent
        while parent_name:
            parent = HSM.get(parent_name)
            if parent and event in parent.transitions:
                self.state = parent.transitions[event]
                return True
            parent_name = parent.parent if parent else None
        return False


async def hsm_turn(conv: HSMConversation, user_msg: str) -> str:
    import re
    conv.messages.append({"role": "user", "content": user_msg})
    node = HSM.get(conv.state)
    valid = list(node.transitions.keys()) if node else []

    client = anthropic.AsyncAnthropic()
    system = (
        f"Context path: {' > '.join(conv.ancestors() + [conv.state])}\n"
        f"{conv.full_prompt()}\n"
        f"Valid events: {valid}\n"
        f"Emit [EVENT:<name>] to trigger a state transition."
    )
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        system=system, messages=conv.messages,
    )
    text = resp.content[0].text
    m = re.search(r'\[EVENT:(\w+)\]', text)
    if m:
        event = m.group(1)
        if conv.transition(event):
            print(f"[hsm] event={event} -> state={conv.state}")
        text = text[:m.start()].strip()

    conv.messages.append({"role": "assistant", "content": text})
    return text


async def demo_hsm():
    conv = HSMConversation("order-001")
    for msg in [
        "I have 2 items in my cart. Ready to pay.",
        "Here are my card details: 4111111111111111 12/26 123",
    ]:
        print(f"\nUser [{conv.state}]: {msg}")
        reply = await hsm_turn(conv, msg)
        print(f"Agent: {reply[:100]}")


asyncio.run(demo_hsm())
```

---

## Solution 4 — Persistent State Machine with Redis

Serialize the conversation state to Redis so the agent can resume exactly
where it left off across process restarts, pod failovers, or multi-day pauses.

```python
import anthropic
import asyncio
import json
import redis.asyncio as aioredis
from dataclasses import dataclass, field, asdict
from enum import Enum


class FlowState(Enum):
    COLLECT_INFO   = "collect_info"
    CONFIRM        = "confirm"
    EXECUTE        = "execute"
    DONE           = "done"


FLOW_PROMPTS = {
    FlowState.COLLECT_INFO: "Collect the user's name, destination, and travel dates.",
    FlowState.CONFIRM:      "Summarise the booking details and ask for explicit confirmation.",
    FlowState.EXECUTE:      "Confirm the booking is being processed. Provide confirmation number.",
    FlowState.DONE:         "Booking complete. Offer itinerary or further help.",
}


@dataclass
class PersistentConv:
    conv_id:    str
    state:      str = FlowState.COLLECT_INFO.value
    collected:  dict = field(default_factory=dict)
    messages:   list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PersistentConv":
        return cls(**d)


CONV_TTL = 7 * 86_400   # 7 days


class PersistentStateMachine:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

    def _key(self, conv_id: str) -> str:
        return f"conv:state:{conv_id}"

    async def load(self, conv_id: str) -> PersistentConv:
        data = await self.redis.get(self._key(conv_id))
        if data:
            return PersistentConv.from_dict(json.loads(data))
        return PersistentConv(conv_id=conv_id)

    async def save(self, conv: PersistentConv) -> None:
        await self.redis.set(
            self._key(conv.conv_id),
            json.dumps(conv.to_dict()),
            ex=CONV_TTL,
        )

    async def turn(self, conv_id: str, user_msg: str) -> str:
        import re, uuid
        conv  = await self.load(conv_id)
        state = FlowState(conv.state)
        prompt = FLOW_PROMPTS.get(state, "")

        # Extract any structured data
        import re as _re
        name_m = _re.search(r'my name is ([A-Z][a-z]+ [A-Z][a-z]+)', user_msg, _re.I)
        if name_m:
            conv.collected["name"] = name_m.group(1)

        conv.messages.append({"role": "user", "content": user_msg})
        client = anthropic.AsyncAnthropic()
        system = (
            f"{prompt}\n"
            f"State: {state.value}  Collected: {conv.collected}\n"
            f"Emit [ADVANCE] when ready to move to next state."
        )
        resp = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            system=system, messages=conv.messages,
        )
        text = resp.content[0].text

        if "[ADVANCE]" in text:
            transitions = {
                FlowState.COLLECT_INFO: FlowState.CONFIRM,
                FlowState.CONFIRM:      FlowState.EXECUTE,
                FlowState.EXECUTE:      FlowState.DONE,
            }
            if state in transitions:
                conv.state = transitions[state].value
                if conv.state == FlowState.EXECUTE.value:
                    conv.collected["confirmation"] = f"BK-{uuid.uuid4().hex[:6].upper()}"
                print(f"[persist] advanced to {conv.state}")
            text = text.replace("[ADVANCE]", "").strip()

        conv.messages.append({"role": "assistant", "content": text})
        await self.save(conv)
        return text


async def demo_persistent():
    psm = PersistentStateMachine()
    conv_id = "booking-xyz-001"
    for msg in [
        "Hi, I want to book a flight. My name is John Smith, flying London to Tokyo, March 15-22.",
        "Yes, that all looks correct. Please go ahead.",
    ]:
        try:
            reply = await psm.turn(conv_id, msg)
            print(f"Agent: {reply[:100]}")
        except Exception as e:
            print(f"[demo] Redis not available: {e} — skipping persistence demo")
            break


asyncio.run(demo_persistent())
```

---

## Solution 5 — Tool-Use State Machine (Model Drives Transitions)

Let the model itself drive state transitions by calling a `transition_state`
tool. This keeps transition logic in the model's reasoning rather than regex
parsing of response text.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field


STATES = {
    "greeting":     "Welcome the user and ask how you can help today.",
    "qualification": "Understand the user's goal and whether this is a good fit.",
    "demo":         "Walk through a product demo tailored to the user's goal.",
    "pricing":      "Present pricing options relevant to the user's use case.",
    "close":        "Address objections and ask for the sale.",
    "follow_up":    "Schedule a follow-up and send recap.",
}

VALID_TRANSITIONS = {
    "greeting":      ["qualification"],
    "qualification": ["demo", "follow_up"],
    "demo":          ["pricing", "follow_up"],
    "pricing":       ["close", "follow_up"],
    "close":         ["follow_up"],
    "follow_up":     [],
}

TRANSITION_TOOL = {
    "name": "transition_state",
    "description": "Advance the conversation to the next state when the current state objective is complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "target_state": {
                "type": "string",
                "enum": list(STATES.keys()),
                "description": "The state to transition to.",
            },
            "reason": {"type": "string", "description": "Why transitioning now."},
        },
        "required": ["target_state", "reason"],
    },
}


@dataclass
class SalesConversation:
    conv_id:  str
    state:    str = "greeting"
    messages: list = field(default_factory=list)

    def transition(self, target: str) -> bool:
        if target in VALID_TRANSITIONS.get(self.state, []):
            print(f"[state-tool] {self.state} -> {target}")
            self.state = target
            return True
        print(f"[state-tool] invalid transition {self.state} -> {target}")
        return False


async def sales_turn(conv: SalesConversation, user_msg: str) -> str:
    conv.messages.append({"role": "user", "content": user_msg})
    client = anthropic.AsyncAnthropic()
    system = (
        f"You are a sales agent.\n"
        f"Current state: {conv.state}\n"
        f"Objective: {STATES[conv.state]}\n"
        f"Valid next states: {VALID_TRANSITIONS.get(conv.state, [])}\n"
        f"Call transition_state when the current objective is complete."
    )

    while True:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            tools=[TRANSITION_TOOL],
            messages=conv.messages,
        )

        tool_calls = [b for b in resp.content if b.type == "tool_use"]

        if not tool_calls or resp.stop_reason == "end_turn":
            text = next((b.text for b in resp.content if hasattr(b, "text")), "")
            conv.messages.append({"role": "assistant", "content": resp.content})
            return text

        # Handle transition tool calls
        tool_results = []
        for tc in tool_calls:
            success = conv.transition(tc.input["target_state"])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": "Transitioned." if success else "Invalid transition.",
            })
            if success:
                system = (
                    f"You are a sales agent.\n"
                    f"Current state: {conv.state}\n"
                    f"Objective: {STATES[conv.state]}"
                )

        conv.messages.append({"role": "assistant", "content": resp.content})
        conv.messages.append({"role": "user",      "content": tool_results})


async def demo_sales():
    conv = SalesConversation("sales-001")
    for msg in [
        "Hi, we're a 50-person startup looking for an AI platform.",
        "Looks good! What does pricing look like?",
    ]:
        print(f"\nUser [{conv.state}]: {msg}")
        reply = await sales_turn(conv, msg)
        print(f"Agent: {reply[:100]}")


asyncio.run(demo_sales())
```

---

## Solution 6 — State Machine with Timeout and Expiry

States have configurable timeouts. If the user doesn't respond within the
window, the state machine automatically advances to a timeout state or sends
a nudge message.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TimedState:
    name:          str
    prompt:        str
    timeout_s:     float = 300.0     # 5 min default
    on_timeout:    str = "abandoned"  # target state on timeout
    nudge_at_s:    float | None = 60.0  # send nudge after 60s silence


TIMED_STATES = {
    "awaiting_info": TimedState(
        "awaiting_info",
        "Ask the user for their account number and issue description.",
        timeout_s=120.0, on_timeout="abandoned", nudge_at_s=60.0,
    ),
    "awaiting_confirm": TimedState(
        "awaiting_confirm",
        "Ask the user to confirm the action before proceeding.",
        timeout_s=60.0, on_timeout="expired", nudge_at_s=30.0,
    ),
    "processing": TimedState(
        "processing", "Tell the user their request is being processed.",
        timeout_s=30.0, on_timeout="processing_timeout",
    ),
    "abandoned": TimedState("abandoned", "The session timed out. Offer to restart.", timeout_s=float("inf")),
    "expired":   TimedState("expired",   "The confirmation window expired.", timeout_s=float("inf")),
    "processing_timeout": TimedState("processing_timeout", "Processing timed out. Apologise.", timeout_s=float("inf")),
}


@dataclass
class TimedConversation:
    conv_id:      str
    state:        str = "awaiting_info"
    messages:     list = field(default_factory=list)
    last_user_ts: float = field(default_factory=time.monotonic)
    nudge_sent:   bool = False

    def record_user_activity(self) -> None:
        self.last_user_ts = time.monotonic()
        self.nudge_sent = False

    def seconds_idle(self) -> float:
        return time.monotonic() - self.last_user_ts

    def check_timeout(self) -> str | None:
        s = TIMED_STATES.get(self.state)
        if not s:
            return None
        idle = self.seconds_idle()
        if idle >= s.timeout_s:
            print(f"[timeout] state={self.state} idle={idle:.0f}s -> {s.on_timeout}")
            self.state = s.on_timeout
            return "timeout"
        if s.nudge_at_s and idle >= s.nudge_at_s and not self.nudge_sent:
            self.nudge_sent = True
            return "nudge"
        return None


async def timed_turn(conv: TimedConversation, user_msg: str) -> str:
    import re
    conv.record_user_activity()
    timeout_event = conv.check_timeout()

    conv.messages.append({"role": "user", "content": user_msg})
    s = TIMED_STATES.get(conv.state)
    if not s:
        return "Unknown state."

    system = f"{s.prompt}\nState: {conv.state}  Idle: {conv.seconds_idle():.0f}s"
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=256, system=system, messages=conv.messages,
    )
    text = resp.content[0].text

    m = re.search(r'\[TRANSITION:(\w+)\]', text)
    if m and m.group(1) in TIMED_STATES:
        print(f"[timed-state] {conv.state} -> {m.group(1)}")
        conv.state = m.group(1)
        text = text[:m.start()].strip()

    conv.messages.append({"role": "assistant", "content": text})
    return text


async def demo_timed():
    conv = TimedConversation("timed-001")
    for msg in [
        "My account number is 12345 and I can't log in.",
        "Yes, please reset my password.",
    ]:
        print(f"\nUser [{conv.state}] idle={conv.seconds_idle():.0f}s: {msg}")
        reply = await timed_turn(conv, msg)
        print(f"Agent: {reply[:100]}")
        await asyncio.sleep(0.1)


asyncio.run(demo_timed())
```

---

## Comparison

| Approach | Persistence | Guard conditions | Resumable | Model drives transitions | Complexity |
|---|---|---|---|---|---|
| Enum-based state machine | In-memory | No | No | No (regex) | Very low |
| Guard conditions + side effects | In-memory | Yes | No | No (regex) | Low |
| Hierarchical state machine | In-memory | No | No | No (regex) | Medium |
| Redis persistent state | Redis | No | Yes | No (regex) | Medium |
| Tool-use transitions | In-memory | Implicit | No | Yes | Medium |
| Timeout + expiry states | In-memory | No | No | No | Medium |

**Rule of thumb:**
- Simple linear flows → enum state machine (Solution 1) in 30 lines
- Multi-step e-commerce / booking → add guard conditions + side effects (Solution 2)
- Complex nested workflows → hierarchical state machine (Solution 3)
- Multi-day / multi-session conversations → Redis persistent state (Solution 4)
- Model should reason about when to advance → tool-use transitions (Solution 5)
- Async flows where users go idle → timeout states (Solution 6)
