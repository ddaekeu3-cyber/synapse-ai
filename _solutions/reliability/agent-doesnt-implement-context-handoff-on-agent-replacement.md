---
layout: solution
title: "Agent Doesn't Implement Context Handoff on Agent Replacement"
description: "How to seamlessly transfer an agent's working context — conversation state, in-flight tasks, accumulated knowledge — to a replacement agent when the original crashes, is upgraded, or is scaled down."
tags: [reliability, handoff, context, continuity, deployment, stateful]
difficulty: advanced
solution_count: 6
---

## Problem

When an agent process crashes, is restarted during deployment, or is replaced by a new version, the new instance starts with no knowledge of what was being worked on. Users lose conversation context. In-flight tasks are abandoned. The new agent asks the user to repeat themselves or starts over, producing a jarring experience and data loss.

```python
# Bad: stateless restart — every restart loses everything
class Agent:
    def __init__(self):
        self.conversation = []  # lost on restart
        self.current_task = None  # lost on restart
        self.accumulated_facts = {}  # lost on restart
```

---

## Solution 1 — Handoff Document: Serializable Context Snapshot

Before shutdown, serialize the complete agent context to a handoff document that the replacement agent loads at startup.

```python
import asyncio
import json
import time
import signal
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class HandoffDocument:
    session_id: str
    agent_version: str
    serialized_at: float
    conversation_history: list[dict]
    current_task: dict | None
    accumulated_facts: dict
    pending_tool_calls: list[dict]
    user_preferences: dict
    agent_notes: str  # agent's own summary of what it was doing

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "HandoffDocument":
        return cls(**json.loads(raw))

HANDOFF_DIR = Path("/var/lib/agent/handoffs")

class HandoffCapableAgent:
    VERSION = "2.1.0"

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.conversation: list[dict] = []
        self.current_task: dict | None = None
        self.facts: dict = {}
        self.pending_tools: list[dict] = []
        self.preferences: dict = {}
        self._shutting_down = False

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_sigterm)

    def _handle_sigterm(self, sig, frame) -> None:
        print(f"SIGTERM received — preparing handoff for {self.session_id}")
        asyncio.create_task(self._shutdown_with_handoff())

    async def _generate_handoff_notes(self) -> str:
        """Ask the LLM to summarize what it was doing for the next agent."""
        if not self.conversation:
            return "No prior context."
        summary_prompt = (
            "You are about to be replaced by a new agent instance. "
            "Summarize what you were working on in 3-5 sentences so the replacement agent "
            "can continue seamlessly. Be specific about: current task, key facts discovered, "
            "what you were about to do next.\n\n"
            f"Conversation so far: {json.dumps(self.conversation[-10:])}\n"
            f"Current task: {self.current_task}\n"
            f"Accumulated facts: {self.facts}"
        )
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        return response.content[0].text

    async def _shutdown_with_handoff(self) -> None:
        self._shutting_down = True
        notes = await self._generate_handoff_notes()
        doc = HandoffDocument(
            session_id=self.session_id,
            agent_version=self.VERSION,
            serialized_at=time.time(),
            conversation_history=self.conversation,
            current_task=self.current_task,
            accumulated_facts=self.facts,
            pending_tool_calls=self.pending_tools,
            user_preferences=self.preferences,
            agent_notes=notes,
        )
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        path = HANDOFF_DIR / f"{self.session_id}.handoff.json"
        path.write_text(doc.to_json())
        print(f"Handoff document written: {path}")

    @classmethod
    def restore_from_handoff(cls, session_id: str) -> "HandoffCapableAgent | None":
        path = HANDOFF_DIR / f"{session_id}.handoff.json"
        if not path.exists():
            return None
        doc = HandoffDocument.from_json(path.read_text())
        agent = cls(session_id)
        agent.conversation = doc.conversation_history
        agent.current_task = doc.current_task
        agent.facts = doc.accumulated_facts
        agent.pending_tools = doc.pending_tool_calls
        agent.preferences = doc.user_preferences
        print(f"Restored from handoff (v{doc.agent_version}): {doc.agent_notes[:100]}")
        path.unlink()  # consume the handoff document
        return agent

# Usage
async def start_or_resume(session_id: str) -> HandoffCapableAgent:
    agent = HandoffCapableAgent.restore_from_handoff(session_id)
    if agent:
        print("Resumed from handoff document")
    else:
        agent = HandoffCapableAgent(session_id)
        print("Started fresh agent")
    return agent
```

---

## Solution 2 — In-Flight Task Resumption with Checkpoint Protocol

For long-running tasks, checkpoint progress at each completed subtask. The replacement agent can resume from the last checkpoint rather than restarting from scratch.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
import redis.asyncio as aioredis

redis = aioredis.from_url("redis://localhost:6379", decode_responses=True)

class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    CHECKPOINT = "checkpoint"
    COMPLETE = "complete"
    FAILED = "failed"

@dataclass
class TaskCheckpoint:
    task_id: str
    session_id: str
    agent_id: str
    status: str
    completed_steps: list[str]
    remaining_steps: list[str]
    step_results: dict
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

CHECKPOINT_KEY = "agent:checkpoint:{task_id}"

async def save_checkpoint(cp: TaskCheckpoint) -> None:
    key = CHECKPOINT_KEY.format(task_id=cp.task_id)
    cp.updated_at = time.time()
    await redis.setex(key, 86400, json.dumps(cp.to_dict()))

async def load_checkpoint(task_id: str) -> TaskCheckpoint | None:
    key = CHECKPOINT_KEY.format(task_id=task_id)
    raw = await redis.get(key)
    if not raw:
        return None
    d = json.loads(raw)
    return TaskCheckpoint(**d)

async def execute_resumable_task(
    task_id: str,
    session_id: str,
    agent_id: str,
    all_steps: list[str],
    step_executor: callable,
) -> dict:
    # Try to resume from checkpoint
    cp = await load_checkpoint(task_id)
    if cp:
        print(f"Resuming task {task_id} from checkpoint: "
              f"{len(cp.completed_steps)}/{len(all_steps)} steps done")
        completed = set(cp.completed_steps)
        results = cp.step_results
    else:
        cp = TaskCheckpoint(
            task_id=task_id,
            session_id=session_id,
            agent_id=agent_id,
            status=TaskStatus.IN_PROGRESS.value,
            completed_steps=[],
            remaining_steps=all_steps[:],
            step_results={},
            created_at=time.time(),
            updated_at=time.time(),
        )
        completed = set()
        results = {}

    for step in all_steps:
        if step in completed:
            continue  # skip already-completed steps

        try:
            print(f"Executing step: {step}")
            result = await step_executor(step, results)
            results[step] = result
            completed.add(step)

            # Checkpoint after each step
            cp.completed_steps = list(completed)
            cp.remaining_steps = [s for s in all_steps if s not in completed]
            cp.step_results = results
            cp.status = TaskStatus.CHECKPOINT.value
            await save_checkpoint(cp)

        except asyncio.CancelledError:
            # Save checkpoint before dying
            cp.completed_steps = list(completed)
            cp.step_results = results
            cp.status = TaskStatus.CHECKPOINT.value
            await save_checkpoint(cp)
            raise

    cp.status = TaskStatus.COMPLETE.value
    await save_checkpoint(cp)
    return results

# Example: research task with checkpointed steps
async def demo():
    task_id = "research-task-001"
    steps = ["search_papers", "summarize_abstracts", "identify_themes", "write_report"]

    async def step_fn(step: str, prior_results: dict) -> str:
        await asyncio.sleep(0.1)  # simulate work
        return f"result of {step}"

    results = await execute_resumable_task(
        task_id, "session-abc", "agent-001", steps, step_fn
    )
    print(f"Task complete: {list(results.keys())}")

asyncio.run(demo())
```

---

## Solution 3 — Warm Handoff via Overlap Period

Run the old and new agent instances simultaneously for a brief overlap period. The old instance transfers context directly to the new instance while both are running, then gracefully exits.

```python
import asyncio
import httpx
import json
from dataclasses import dataclass
from typing import Any

@dataclass
class AgentContext:
    session_id: str
    conversation: list[dict]
    active_tools: list[str]
    user_state: dict
    agent_notes: str

class WarmHandoffAgent:
    def __init__(self, agent_id: str, session_id: str,
                 handoff_receiver_url: str = None):
        self.agent_id = agent_id
        self.session_id = session_id
        self._handoff_url = handoff_receiver_url
        self._context = AgentContext(session_id, [], [], {}, "")
        self._accepting_new_requests = True

    async def initiate_warm_handoff(self, successor_url: str) -> bool:
        """Transfer context to successor while still running."""
        print(f"[{self.agent_id}] Initiating warm handoff to {successor_url}")

        # Step 1: Stop accepting new requests from load balancer
        self._accepting_new_requests = False
        print(f"[{self.agent_id}] Stopped accepting new requests")

        # Step 2: Wait for in-flight requests to complete (drain)
        await asyncio.sleep(2.0)  # In production: wait for actual in-flight counter to hit 0

        # Step 3: Transfer context to successor
        context_payload = {
            "session_id": self._context.session_id,
            "conversation": self._context.conversation,
            "active_tools": self._context.active_tools,
            "user_state": self._context.user_state,
            "agent_notes": self._context.agent_notes,
            "transferred_from": self.agent_id,
            "transfer_reason": "planned_replacement",
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{successor_url}/accept-handoff",
                    json=context_payload,
                    timeout=10.0,
                )
                response.raise_for_status()
                print(f"[{self.agent_id}] Handoff accepted by successor")
                return True
        except Exception as e:
            print(f"[{self.agent_id}] Handoff failed: {e} — persisting to disk")
            # Fallback: save to disk for cold restore
            return False

    async def accept_handoff(self, context_payload: dict) -> None:
        """Receive context from predecessor."""
        self._context = AgentContext(
            session_id=context_payload["session_id"],
            conversation=context_payload["conversation"],
            active_tools=context_payload["active_tools"],
            user_state=context_payload["user_state"],
            agent_notes=context_payload["agent_notes"],
        )
        print(
            f"[{self.agent_id}] Accepted handoff from {context_payload['transferred_from']}: "
            f"{len(self._context.conversation)} conversation turns, "
            f"notes: {self._context.agent_notes[:50]}"
        )
        # Inject a system message so the LLM knows it's continuing
        self._context.conversation.insert(0, {
            "role": "system",
            "content": (
                f"[HANDOFF] You are continuing a conversation started by a previous agent instance. "
                f"Context: {self._context.agent_notes}"
            )
        })

# FastAPI endpoint for receiving handoffs
from fastapi import FastAPI, Request
app = FastAPI()
active_agent: WarmHandoffAgent | None = None

@app.post("/accept-handoff")
async def accept_handoff_endpoint(request: Request):
    payload = await request.json()
    if active_agent:
        await active_agent.accept_handoff(payload)
        return {"status": "accepted", "agent_id": active_agent.agent_id}
    return {"status": "no_active_agent"}, 503
```

---

## Solution 4 — Conversation Compression for Compact Handoff

When the conversation history is long, use the LLM to compress it into a dense summary before handoff. The replacement agent starts with the summary instead of the full history.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

COMPRESS_PROMPT = """\
You are preparing a handoff document for a replacement AI agent.
Compress the following conversation history into a compact summary that preserves:
1. The user's original goal and current state
2. All decisions made and their rationale
3. Key facts, entities, and preferences discovered
4. What was being worked on when the conversation paused
5. Critical context the next agent MUST know

Conversation history (last {n} turns):
{history}

Return a JSON object:
{{
  "user_goal": "...",
  "current_task": "...",
  "key_facts": {{"entity": "value", ...}},
  "decisions_made": ["...", "..."],
  "next_steps": ["...", "..."],
  "critical_context": "...",
  "compressed_history": "...(dense narrative of what happened)..."
}}"""

async def compress_conversation(history: list[dict], max_turns: int = 20) -> dict:
    """Compress conversation history into a structured handoff summary."""
    recent = history[-max_turns:]
    history_str = "\n".join(
        f"{msg['role'].upper()}: {msg['content'][:200]}"
        for msg in recent
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": COMPRESS_PROMPT.format(n=len(recent), history=history_str)
        }],
    )
    text = response.content[0].text.strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"compressed_history": text, "parse_error": True}

async def restore_from_compressed_handoff(summary: dict) -> list[dict]:
    """Convert compressed handoff back into conversation history for the new agent."""
    system_msg = {
        "role": "user",
        "content": (
            f"[HANDOFF CONTEXT]\n"
            f"User goal: {summary.get('user_goal', 'unknown')}\n"
            f"Current task: {summary.get('current_task', 'none')}\n"
            f"Key facts: {json.dumps(summary.get('key_facts', {}))}\n"
            f"Decisions made: {'; '.join(summary.get('decisions_made', []))}\n"
            f"What happened: {summary.get('compressed_history', '')}\n"
            f"Next steps: {'; '.join(summary.get('next_steps', []))}\n\n"
            f"Please continue assisting the user from this point."
        )
    }
    ack = {
        "role": "assistant",
        "content": (
            f"I understand. I'm continuing the session where the previous agent left off. "
            f"The user's goal is: {summary.get('user_goal', 'unclear')}. "
            f"I'll pick up from: {summary.get('current_task', 'the beginning')}."
        )
    }
    return [system_msg, ack]

async def demo():
    history = [
        {"role": "user", "content": "I need help building a Python web scraper"},
        {"role": "assistant", "content": "I'll help you build a web scraper. What site are you targeting?"},
        {"role": "user", "content": "I want to scrape news headlines from BBC"},
        {"role": "assistant", "content": "For BBC, I recommend using requests + BeautifulSoup. Here's the plan..."},
        {"role": "user", "content": "But I need it to handle JavaScript too"},
        {"role": "assistant", "content": "In that case, use Playwright. Let me write the code..."},
    ]

    summary = await compress_conversation(history)
    print("Compressed summary:", json.dumps(summary, indent=2)[:500])

    restored = await restore_from_compressed_handoff(summary)
    print(f"\nRestored as {len(restored)} synthetic history messages")

asyncio.run(demo())
```

---

## Solution 5 — Zero-Downtime Blue-Green Agent Handoff

Run two agent versions (blue and new green) simultaneously. Route new sessions to green; migrate existing blue sessions to green one at a time using context transfer.

```python
import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

@dataclass
class SessionRecord:
    session_id: str
    agent_version: str  # "blue" or "green"
    context: dict
    last_active: float

class BlueGreenAgentRouter:
    def __init__(self):
        self._sessions: dict[str, SessionRecord] = {}
        self._blue_agents: dict[str, Any] = {}   # running blue instances
        self._green_agents: dict[str, Any] = {}  # running green instances
        self._migration_pct: float = 0.0  # 0-100: % of new sessions going to green

    def set_green_rollout(self, pct: float) -> None:
        """Gradually shift new sessions to green."""
        self._migration_pct = max(0, min(100, pct))
        print(f"Green rollout: {self._migration_pct:.0f}% of new sessions")

    def assign_agent(self, session_id: str) -> str:
        """Assign new session to blue or green based on rollout %."""
        if session_id in self._sessions:
            return self._sessions[session_id].agent_version
        version = "green" if random.random() * 100 < self._migration_pct else "blue"
        return version

    async def migrate_session_to_green(self, session_id: str) -> bool:
        """Migrate an existing blue session to green."""
        record = self._sessions.get(session_id)
        if not record or record.agent_version == "green":
            return False

        blue_agent = self._blue_agents.get(session_id)
        if not blue_agent:
            return False

        # Extract context from blue agent
        context = await self._extract_context(session_id, blue_agent)

        # Create green agent with transferred context
        green_agent = await self._create_green_agent(session_id, context)
        self._green_agents[session_id] = green_agent

        # Update routing
        record.agent_version = "green"
        record.context = context

        # Shut down blue agent for this session
        del self._blue_agents[session_id]
        print(f"Session {session_id}: migrated blue->green")
        return True

    async def migrate_all_sessions(self, batch_size: int = 10,
                                    delay: float = 1.0) -> None:
        """Gradually migrate all blue sessions to green."""
        blue_sessions = [
            sid for sid, rec in self._sessions.items()
            if rec.agent_version == "blue"
        ]
        print(f"Migrating {len(blue_sessions)} sessions from blue to green")

        for i in range(0, len(blue_sessions), batch_size):
            batch = blue_sessions[i:i+batch_size]
            await asyncio.gather(*[self.migrate_session_to_green(s) for s in batch])
            print(f"Migrated batch {i//batch_size + 1}, waiting {delay}s...")
            await asyncio.sleep(delay)

        print("All sessions migrated to green")

    async def _extract_context(self, session_id: str, agent) -> dict:
        return getattr(agent, "_context", {})

    async def _create_green_agent(self, session_id: str, context: dict) -> Any:
        # Simplified: in production, instantiate the new agent version
        return {"session_id": session_id, "_context": context, "version": "green"}

router = BlueGreenAgentRouter()
router.set_green_rollout(10)    # 10% new sessions go to green
# Later: router.set_green_rollout(50)
# Later: router.set_green_rollout(100)
# Then: await router.migrate_all_sessions()
```

---

## Solution 6 — Handoff-Aware User Message: Transparent Context Injection

When no automated handoff is available, inject a transparent context-restoration prompt into the next agent turn so users never need to repeat themselves.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

RESTORATION_SYSTEM = """\
You are continuing a conversation that was interrupted when a previous agent instance stopped.
The following is a summary of what was discussed and accomplished:

PRIOR CONTEXT:
{context_summary}

USER PREFERENCES NOTED:
{preferences}

PENDING ITEMS:
{pending}

When responding:
1. Do not tell the user you are a new instance unless directly asked
2. Continue naturally from where the conversation left off
3. If you need to confirm something from the prior context, ask briefly
4. Priority: complete the pending task before responding to new requests"""

@dataclass
class ConversationSnapshot:
    context_summary: str
    user_preferences: list[str]
    pending_items: list[str]
    last_messages: list[dict]

async def restore_and_continue(
    snapshot: ConversationSnapshot,
    new_user_message: str,
) -> str:
    """Resume an interrupted conversation with context injection."""
    system = RESTORATION_SYSTEM.format(
        context_summary=snapshot.context_summary,
        preferences="; ".join(snapshot.user_preferences) or "none noted",
        pending="; ".join(snapshot.pending_items) or "none",
    )

    # Include last few messages for continuity
    messages = snapshot.last_messages[-6:] + [
        {"role": "user", "content": new_user_message}
    ]

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    )
    return response.content[0].text

async def demo():
    # Simulate a snapshot from a crashed agent
    snapshot = ConversationSnapshot(
        context_summary=(
            "User is building a Python web scraper for BBC news headlines. "
            "We established they need JavaScript support, so we decided to use Playwright. "
            "I was about to write the initial code when the session was interrupted."
        ),
        user_preferences=["Prefers short, practical code examples", "Uses Python 3.12"],
        pending_items=["Write the Playwright scraper code", "Explain how to run it"],
        last_messages=[
            {"role": "user", "content": "I want to scrape BBC news headlines"},
            {"role": "assistant", "content": "For dynamic content like BBC, we should use Playwright. I'll write the code..."},
        ],
    )

    response = await restore_and_continue(
        snapshot,
        "Sorry I got disconnected — can you continue where we left off?"
    )
    print(f"Restored response: {response[:200]}")

asyncio.run(demo())
```

---

## Comparison

| Approach | Data Loss Risk | User Disruption | Works Offline | Migration Speed | Best For |
|---|---|---|---|---|---|
| Handoff document | Low | None if completed | **Yes** | Instant on restart | SIGTERM-triggered replacements |
| Checkpoint protocol | **Minimal** (per-step) | None | **Yes** | Resume from last step | Long-running tasks |
| Warm handoff (overlap) | **None** | **None** | No | Real-time transfer | Planned rolling upgrades |
| Conversation compression | Low (lossy) | None | **Yes** | Fast (single LLM call) | Long conversation histories |
| Blue-green migration | **None** | **None** | No | Gradual | Zero-downtime version upgrades |
| Context injection | Medium (manual) | Low | **Yes** | Instant | Fallback when automated fails |
