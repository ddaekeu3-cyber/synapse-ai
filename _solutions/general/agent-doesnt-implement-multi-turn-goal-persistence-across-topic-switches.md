---
layout: solution
title: "Agent Doesn't Implement Multi-Turn Goal Persistence Across Topic Switches"
category: general
description: "Maintain awareness of a user's original goal when they temporarily switch topics or go on tangents — so the agent can re-anchor and complete what was originally asked."
tags: [general, goal-tracking, multi-turn, conversation, context, ux, focus]
---

## Problem

Users frequently digress. A developer asks "help me design a REST API" then spends five messages asking about Docker networking, then comes back expecting the agent to continue the API design. A naive agent treats each message independently — by the time the user returns to the original topic, the agent has completely lost the thread. The agent either restarts from scratch or fails to connect the tangent back to the original request.

```python
# Naive: every message treated as standalone
def respond(message: str, history: list) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=history + [{"role": "user", "content": message}],
    )
    return r.content[0].text  # no awareness of the original goal
```

## Solution Options

### Option 1: Explicit Goal Register with Extraction and Recall

Extract the user's primary goal at the start of a conversation. Inject it as a persistent anchor into every subsequent message, even through topic switches.

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class ConversationGoal:
    original_request: str
    extracted_goal: str
    status: str = "in_progress"  # "in_progress" | "completed" | "abandoned"

client = anthropic.Anthropic()

EXTRACT_GOAL_PROMPT = """Extract the user's primary goal from this opening message.
State it as a single, concrete objective in one sentence.
Message: {message}
Return only the goal statement, nothing else."""

GOAL_ANCHOR_SYSTEM = """You are a helpful assistant.

Primary goal for this conversation: {goal}

Even if the user asks tangential questions, keep this goal in mind. When the tangent is resolved,
gently steer back toward completing the primary goal."""

class GoalPersistentAgent:
    def __init__(self):
        self.goal: ConversationGoal | None = None
        self.history: list[dict] = []
        self.turn_count: int = 0

    def _extract_goal(self, first_message: str) -> str:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": EXTRACT_GOAL_PROMPT.format(message=first_message)}],
        )
        return r.content[0].text.strip()

    def respond(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        self.turn_count += 1

        # Extract goal on first turn
        if self.turn_count == 1:
            goal_text = self._extract_goal(user_message)
            self.goal = ConversationGoal(original_request=user_message, extracted_goal=goal_text)
            print(f"[GOAL] Registered: {goal_text}")

        system = (
            GOAL_ANCHOR_SYSTEM.format(goal=self.goal.extracted_goal)
            if self.goal else "You are a helpful assistant."
        )
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=self.history,
        )
        reply = r.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def summarize_goal_status(self) -> str:
        if not self.goal:
            return "No goal registered."
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content":
                f"Original goal: {self.goal.extracted_goal}\n"
                f"Conversation so far: {len(self.history)} turns.\n"
                "Has the goal been completed? What remains? Answer in 2 sentences."}],
        )
        return r.content[0].text


# Demo
agent = GoalPersistentAgent()
turns = [
    "I want to design a REST API for a task management app with users, tasks, and projects.",
    "Actually, first — how does Docker networking work? I need to understand that first.",
    "What's the difference between bridge and host networking in Docker?",
    "OK I think I understand Docker now. Let's get back to the API design.",
]
for msg in turns:
    print(f"User: {msg}")
    print(f"Agent: {agent.respond(msg)[:200]}\n")

print("Goal status:", agent.summarize_goal_status())

# Expected Token Savings: Goal injection adds ~30 tokens/turn; prevents multi-turn context loss worth many more
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Goal Stack with Push/Pop for Nested Sub-Goals

Track a stack of goals. When a user introduces a sub-goal, push it onto the stack. When resolved, pop back to the parent goal and resume.

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class Goal:
    description: str
    context: str
    depth: int
    resolved: bool = False

class GoalStackAgent:
    def __init__(self):
        self.stack: list[Goal] = []
        self.history: list[dict] = []
        self.client = anthropic.Anthropic()

    CLASSIFY_PROMPT = """Classify this user message in context of the current goal.

Current goal: {current_goal}
User message: {message}

Is the user:
A) Working directly toward the current goal
B) Introducing a new sub-goal/tangent that needs to be resolved first
C) Signaling they want to return to a parent goal

Return JSON: {{"classification": "A"|"B"|"C", "sub_goal": "<if B, describe the sub-goal briefly>"}}"""

    def _classify(self, message: str) -> dict:
        import json
        current = self.stack[-1].description if self.stack else "general conversation"
        r = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": self.CLASSIFY_PROMPT.format(
                current_goal=current, message=message,
            )}],
        )
        try:
            return json.loads(r.content[0].text)
        except Exception:
            return {"classification": "A", "sub_goal": ""}

    def _build_system(self) -> str:
        if not self.stack:
            return "You are a helpful assistant."
        goal_chain = " → ".join(f"[{g.description}]" for g in self.stack)
        current = self.stack[-1]
        lines = [
            f"Goal stack (bottom → top): {goal_chain}",
            f"Current focus: {current.description}",
        ]
        if len(self.stack) > 1:
            parent = self.stack[-2]
            lines.append(f"Parent goal to return to: {parent.description}")
        lines.append("Help the user with the current focus, but remember the parent goals.")
        return "\n".join(lines)

    def respond(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        if not self.stack:
            # First message: register as root goal
            self.stack.append(Goal(description=user_message[:80], context=user_message, depth=0))
            print(f"[GOAL STACK] Root goal registered: {self.stack[0].description}")
        else:
            classification = self._classify(user_message)
            cls = classification["classification"]
            if cls == "B":
                sub_goal = classification.get("sub_goal", user_message[:60])
                self.stack.append(Goal(description=sub_goal, context=user_message, depth=len(self.stack)))
                print(f"[GOAL STACK] Push depth={len(self.stack)-1}: {sub_goal}")
            elif cls == "C" and len(self.stack) > 1:
                popped = self.stack.pop()
                print(f"[GOAL STACK] Pop: resolved '{popped.description}', resuming '{self.stack[-1].description}'")

        r = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=self._build_system(),
            messages=self.history,
        )
        reply = r.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply


# Demo
agent = GoalStackAgent()
conversation = [
    "I want to build a Python CLI tool for managing SSH keys.",
    "Before we start, can you explain how RSA key pairs work?",
    "And what's the difference between RSA and ED25519?",
    "OK I think I understand keys now. Let's get back to the CLI tool.",
]
for msg in conversation:
    print(f"User: {msg}")
    print(f"Agent: {agent.respond(msg)[:200]}\n")
    print(f"Stack depth: {len(agent.stack)}")

# Expected Token Savings: Stack state adds ~50 tokens/turn; enables complex multi-topic conversations without restart
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Async Goal Monitor with Periodic Re-Anchoring

A background coroutine monitors conversation drift. When drift is detected (original goal not mentioned in N turns), it injects a re-anchoring reminder into the system prompt.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

@dataclass
class DriftState:
    original_goal: str
    turns_since_goal_mentioned: int = 0
    re_anchor_threshold: int = 3
    is_drifted: bool = False

client = anthropic.AsyncAnthropic()

async def _goal_mentioned(goal: str, recent_messages: list[str]) -> bool:
    combined = " ".join(recent_messages[-3:]).lower()
    # Quick heuristic: check keyword overlap
    goal_keywords = set(goal.lower().split()) - {"a", "an", "the", "to", "for", "of", "and"}
    recent_words = set(combined.split())
    overlap = goal_keywords & recent_words
    return len(overlap) >= 2  # at least 2 goal keywords present

async def _build_reanchor_message(goal: str, drift_turns: int) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content":
            f"Write a gentle one-sentence reminder to the user that we were working on: '{goal}'. "
            f"We've been on a tangent for {drift_turns} turns. Be friendly, not pushy."}],
    )
    return r.content[0].text

class DriftAwareAgent:
    def __init__(self, reanchor_threshold: int = 3):
        self.drift = DriftState(original_goal="", re_anchor_threshold=reanchor_threshold)
        self.history: list[dict] = []
        self.turn_count: int = 0

    async def respond(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        self.turn_count += 1

        if self.turn_count == 1:
            self.drift.original_goal = user_message[:120]

        # Check for drift
        recent_user_msgs = [m["content"] for m in self.history if m["role"] == "user"]
        goal_present = await _goal_mentioned(self.drift.original_goal, recent_user_msgs)
        if goal_present:
            self.drift.turns_since_goal_mentioned = 0
            self.drift.is_drifted = False
        else:
            self.drift.turns_since_goal_mentioned += 1
            if self.drift.turns_since_goal_mentioned >= self.drift.re_anchor_threshold:
                self.drift.is_drifted = True

        # Build system prompt with optional re-anchor
        system_parts = [f"Primary goal: {self.drift.original_goal}"]
        if self.drift.is_drifted:
            reminder = await _build_reanchor_message(
                self.drift.original_goal,
                self.drift.turns_since_goal_mentioned,
            )
            system_parts.append(f"[RE-ANCHOR]: {reminder}")
            print(f"[DRIFT] {self.drift.turns_since_goal_mentioned} turns off-goal → re-anchoring")

        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="\n".join(system_parts),
            messages=self.history,
        )
        reply = r.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply


async def main():
    agent = DriftAwareAgent(reanchor_threshold=2)
    conversation = [
        "Help me write a Python script that monitors disk usage and sends email alerts.",
        "By the way, what email library should I use for Python?",
        "What's better, smtplib or sendgrid?",
        "And can you explain SMTP authentication?",
        "OK what about the disk monitoring part?",
    ]
    for msg in conversation:
        print(f"User: {msg}")
        reply = await agent.respond(msg)
        print(f"Agent: {reply[:200]}\n")

asyncio.run(main())

# Expected Token Savings: Re-anchor adds ~30 tokens only when drifted (threshold 3+ turns); prevents full restart
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Goal Completion Tracker with Progress Milestones

Decompose the original goal into milestones at conversation start. Track completion state across topic switches and show remaining milestones when the user returns.

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class Milestone:
    description: str
    completed: bool = False

@dataclass
class GoalPlan:
    original_request: str
    milestones: list[Milestone]

    def remaining(self) -> list[Milestone]:
        return [m for m in self.milestones if not m.completed]

    def completed_count(self) -> int:
        return sum(1 for m in self.milestones if m.completed)

    def progress_summary(self) -> str:
        done = self.completed_count()
        total = len(self.milestones)
        remaining = [m.description for m in self.remaining()]
        return f"{done}/{total} milestones completed. Remaining: {remaining}"

client = anthropic.Anthropic()

DECOMPOSE_PROMPT = """Break this user request into 3-5 concrete milestones to complete.
Request: {request}
Return JSON array: ["milestone 1", "milestone 2", ...]"""

PROGRESS_PROMPT = """Based on this conversation turn, which milestones were just completed?
Milestones: {milestones}
Assistant's last response: {response}
Return JSON array of indices (0-based) of newly completed milestones: [0, 2, ...]"""

class MilestoneTrackingAgent:
    def __init__(self):
        self.plan: GoalPlan | None = None
        self.history: list[dict] = []

    def _decompose_goal(self, request: str) -> GoalPlan:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": DECOMPOSE_PROMPT.format(request=request)}],
        )
        milestones = json.loads(r.content[0].text)
        return GoalPlan(
            original_request=request,
            milestones=[Milestone(m) for m in milestones],
        )

    def _check_progress(self, assistant_response: str) -> list[int]:
        if not self.plan:
            return []
        remaining = [(i, m.description) for i, m in enumerate(self.plan.milestones) if not m.completed]
        if not remaining:
            return []
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": PROGRESS_PROMPT.format(
                milestones=json.dumps([m for _, m in remaining]),
                response=assistant_response[:400],
            )}],
        )
        try:
            raw_indices = json.loads(r.content[0].text)
            # Map back to actual indices in full milestone list
            real_indices = [remaining[i][0] for i in raw_indices if i < len(remaining)]
            return real_indices
        except Exception:
            return []

    def _build_system(self) -> str:
        if not self.plan:
            return "You are a helpful assistant."
        remaining = self.plan.remaining()
        if not remaining:
            return f"You have fully completed the user's goal: {self.plan.original_request}"
        done = self.plan.completed_count()
        total = len(self.plan.milestones)
        next_milestone = remaining[0].description
        return (
            f"Original goal: {self.plan.original_request}\n"
            f"Progress: {done}/{total} milestones completed.\n"
            f"Next milestone to work toward: {next_milestone}\n"
            f"If the user asks about something else, answer it, but keep the overall goal in mind."
        )

    def respond(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        if not self.plan:
            self.plan = self._decompose_goal(user_message)
            print(f"[MILESTONES] Plan:")
            for i, m in enumerate(self.plan.milestones):
                print(f"  {i+1}. {m.description}")

        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=self._build_system(),
            messages=self.history,
        )
        reply = r.content[0].text
        self.history.append({"role": "assistant", "content": reply})

        # Update progress
        newly_done = self._check_progress(reply)
        for idx in newly_done:
            self.plan.milestones[idx].completed = True
            print(f"[MILESTONE DONE] {self.plan.milestones[idx].description}")

        print(f"[PROGRESS] {self.plan.progress_summary()}")
        return reply


agent = MilestoneTrackingAgent()
conversation = [
    "Build me a FastAPI app with user authentication and a todos endpoint.",
    "What's the difference between JWT and session-based auth?",
    "Let's implement the user auth part now.",
    "Now can we add the todos endpoint?",
]
for msg in conversation:
    print(f"\nUser: {msg}")
    reply = agent.respond(msg)
    print(f"Agent: {reply[:200]}")

# Expected Token Savings: Milestone tracking adds ~80 tokens/turn; prevents restarting completed sub-tasks
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Intent Fingerprinting for Automatic Topic-Return Detection

Create a fingerprint of the original goal. Automatically detect when the user's message aligns with the original intent and surface what still needs to be done.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class IntentFingerprint:
    original_text: str
    keywords: list[str]
    domain: str     # e.g. "software", "writing", "analysis"
    output_type: str  # e.g. "code", "document", "explanation"

client = anthropic.Anthropic()

FINGERPRINT_PROMPT = """Extract the intent fingerprint from this request.
Request: {request}
Return JSON: {{
  "keywords": ["<3-5 most distinctive words>"],
  "domain": "<domain>",
  "output_type": "<expected output type>"
}}"""

ALIGNMENT_PROMPT = """Does this user message align with the original goal intent?

Original goal keywords: {keywords}
Original domain: {domain}
Current message: {message}

Return JSON: {{"aligned": true/false, "alignment_score": <0.0-1.0>}}"""

def fingerprint_intent(text: str) -> IntentFingerprint:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": FINGERPRINT_PROMPT.format(request=text)}],
    )
    data = json.loads(r.content[0].text)
    return IntentFingerprint(
        original_text=text,
        keywords=data["keywords"],
        domain=data["domain"],
        output_type=data["output_type"],
    )

def check_alignment(fp: IntentFingerprint, message: str) -> tuple[bool, float]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": ALIGNMENT_PROMPT.format(
            keywords=fp.keywords,
            domain=fp.domain,
            message=message,
        )}],
    )
    try:
        data = json.loads(r.content[0].text)
        return bool(data["aligned"]), float(data["alignment_score"])
    except Exception:
        return False, 0.0


class IntentFingerprintAgent:
    def __init__(self):
        self.fingerprint: IntentFingerprint | None = None
        self.history: list[dict] = []
        self.digressing_turns: int = 0

    def respond(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        if not self.fingerprint:
            self.fingerprint = fingerprint_intent(user_message)
            print(f"[FINGERPRINT] keywords={self.fingerprint.keywords} domain={self.fingerprint.domain}")
            system = f"Help the user with: {user_message}"
        else:
            aligned, score = check_alignment(self.fingerprint, user_message)
            if aligned:
                self.digressing_turns = 0
                completion_hint = (
                    f"\nUser is returning to original goal. Remind yourself of what was accomplished "
                    f"and what remains to complete: {self.fingerprint.original_text[:80]}"
                    if self.digressing_turns == 0 else ""
                )
                system = f"Original goal: {self.fingerprint.original_text}{completion_hint}"
            else:
                self.digressing_turns += 1
                system = (
                    f"Original goal: {self.fingerprint.original_text}\n"
                    f"User is currently on a tangent (turn {self.digressing_turns}). "
                    f"Answer their tangent helpfully but be ready to return to the original goal."
                )
            print(f"[ALIGN] score={score:.2f} digressing={self.digressing_turns}")

        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=self.history,
        )
        reply = r.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply


agent = IntentFingerprintAgent()
for msg in [
    "Write a Python function to parse JSON config files with validation.",
    "What's the difference between JSON and YAML?",
    "Can you show me a YAML example?",
    "OK now help me write the JSON parser function.",
]:
    print(f"User: {msg}")
    print(f"Agent: {agent.respond(msg)[:200]}\n")

# Expected Token Savings: Fingerprint check ~64 tokens; alignment routing saves multi-turn restarts
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Multi-Goal Session with Named Goal Slots

Support users who juggle multiple simultaneous goals. Each goal has a name and can be activated explicitly or via intent detection.

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class NamedGoal:
    name: str
    description: str
    progress_notes: list[str] = field(default_factory=list)
    active: bool = False

    def add_progress(self, note: str) -> None:
        self.progress_notes.append(note)

    def summary(self) -> str:
        notes = "; ".join(self.progress_notes[-3:]) if self.progress_notes else "not started"
        return f"[{self.name}] {self.description} — progress: {notes}"

client = anthropic.Anthropic()

ROUTE_PROMPT = """Which goal does this message relate to? If it introduces a new goal, say "new".

Goals:
{goal_list}

Message: {message}

Return JSON: {{"goal_name": "<name of matching goal or 'new'>", "new_goal_description": "<if new>"}}"""

class MultiGoalAgent:
    def __init__(self):
        self.goals: dict[str, NamedGoal] = {}
        self.active_goal: NamedGoal | None = None
        self.history: list[dict] = []
        self.turn: int = 0

    def _route_message(self, message: str) -> tuple[str, str]:
        if not self.goals:
            return "new", message[:80]
        goal_list = "\n".join(f"- {name}: {g.description}" for name, g in self.goals.items())
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": ROUTE_PROMPT.format(
                goal_list=goal_list, message=message,
            )}],
        )
        try:
            data = json.loads(r.content[0].text)
            return data["goal_name"], data.get("new_goal_description", "")
        except Exception:
            return list(self.goals.keys())[0], ""

    def _register_goal(self, description: str) -> NamedGoal:
        name = f"goal_{len(self.goals) + 1}"
        goal = NamedGoal(name=name, description=description)
        self.goals[name] = goal
        print(f"[MULTI-GOAL] Registered '{name}': {description}")
        return goal

    def _build_system(self) -> str:
        if not self.goals:
            return "You are a helpful assistant."
        all_summaries = "\n".join(g.summary() for g in self.goals.values())
        active_line = (
            f"\nCurrently active goal: {self.active_goal.summary()}"
            if self.active_goal else ""
        )
        return f"User has multiple goals:\n{all_summaries}{active_line}\nFocus on the active goal."

    def _update_progress(self, reply: str) -> None:
        if self.active_goal and len(reply) > 50:
            note = reply[:60].strip().replace("\n", " ")
            self.active_goal.add_progress(note)

    def respond(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        self.turn += 1

        goal_name, new_desc = self._route_message(user_message)
        if goal_name == "new":
            self.active_goal = self._register_goal(new_desc or user_message[:80])
        elif goal_name in self.goals:
            self.active_goal = self.goals[goal_name]
            print(f"[MULTI-GOAL] Switched to '{goal_name}'")

        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=self._build_system(),
            messages=self.history,
        )
        reply = r.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        self._update_progress(reply)
        return reply


agent = MultiGoalAgent()
for msg in [
    "I need to write a blog post about async Python programming.",
    "I also need to build a database schema for a book library app.",
    "Let's work on the blog post first — can you outline it?",
    "Now switch to the database schema — what tables do I need?",
    "Back to the blog post — write the introduction.",
]:
    print(f"User: {msg}")
    reply = agent.respond(msg)
    print(f"Agent ({agent.active_goal.name if agent.active_goal else 'none'}): {reply[:180]}\n")

# Expected Token Savings: Multi-goal routing ~100 tokens/turn; enables parallel workstreams without context loss
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Goal Structure | Topic Switch Handling | Progress Tracking | Best For |
|--------|---------------|----------------------|------------------|----------|
| 1. Goal Register | Single goal string | System prompt injection | No | Simple single-goal conversations |
| 2. Goal Stack | LIFO stack | Push/pop on classification | No | Nested sub-goals, hierarchical tasks |
| 3. Drift Monitor | Single goal + drift counter | Re-anchor after N turns | No | Long conversations with passive monitoring |
| 4. Milestones | Goal + milestone list | Resume via remaining milestones | Yes (milestone-level) | Complex multi-step deliverables |
| 5. Intent Fingerprint | Keyword + domain fingerprint | Alignment score routing | No | Intent-based return detection |
| 6. Multi-Goal Slots | Named goal registry | Per-message routing | Basic notes | Multi-project parallel work |

**Recommended**: Option 4 (milestones) for task-oriented agents. Option 2 (goal stack) for conversational assistants with nested Q&A. Option 6 (multi-goal) for power users managing parallel workstreams.
