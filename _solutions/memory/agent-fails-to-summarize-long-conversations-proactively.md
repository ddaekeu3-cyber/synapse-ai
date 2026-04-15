---
layout: solution
title: "Agent Doesn't Summarize Long Conversations — Context Window Fills Up Mid-Task"
category: memory
description: "The agent starts a complex multi-turn task. After 30 turns, the context is 90% full. Rather than proactively compressing earlier turns, the agent keeps appending until it hits the limit and crashes or truncates. The last truncated turn contains critical instructions. The agent loses track of decisions made 20 turns ago. Proactive summarization never happens — the agent just runs out of context."
tags: [memory, context-window, summarization, compression, multi-turn, long-session, token-budget, context-management]
---

# Agent Doesn't Summarize Long Conversations — Context Window Fills Up Mid-Task

## Symptom
- Agent crashes or truncates mid-task when context window fills
- Agent forgets decisions made early in a long session
- Turn 35 contradicts what was agreed in turn 10 — agent lost the context
- Performance degrades as context grows (attention dilution on very long contexts)
- Context window usage climbs to 95%+ with no compression happening
- Task fails because a critical user instruction from turn 2 was cut off
- No mechanism to preserve important decisions when old turns are dropped

## Root Cause
Agents that simply append every turn to the conversation history will eventually hit the context window limit. The naive fix — silently truncating old turns — loses important information. The correct approach is proactive summarization: when the context reaches a threshold (e.g., 70% full), compress the oldest turns into a summary that preserves key decisions, facts, and constraints. The summary replaces the raw turns, freeing space while preserving the information.

## Fix

### Option 1: Token-budget-aware session — summarize at threshold

```python
import anthropic
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Approximate token counts per model (input context window):
MODEL_CONTEXT_WINDOWS = {
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)

def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
    return total + len(messages) * 4  # Per-message overhead

@dataclass
class ProactivelySummarizingSession:
    """
    Multi-turn session that proactively summarizes when context grows too large.
    Preserves important decisions and facts while freeing context space.
    """
    model: str = "claude-sonnet-4-6"
    summarize_threshold: float = 0.70    # Summarize when 70% full
    keep_recent_turns: int = 6           # Keep last N turns verbatim
    system_prompt: str = ""
    _messages: list[dict] = field(default_factory=list)
    _summary: str = ""                   # Accumulated summary of older turns

    @property
    def _context_window(self) -> int:
        return MODEL_CONTEXT_WINDOWS.get(self.model, 200_000)

    @property
    def _used_tokens(self) -> int:
        base = estimate_tokens(self.system_prompt + self._summary)
        return base + estimate_messages_tokens(self._messages)

    @property
    def _utilization(self) -> float:
        return self._used_tokens / self._context_window

    def _should_summarize(self) -> bool:
        return self._utilization >= self.summarize_threshold

    def _summarize_old_turns(self, client: anthropic.Anthropic):
        """Compress older turns into a summary, keep recent turns verbatim."""
        if len(self._messages) <= self.keep_recent_turns * 2:
            return  # Not enough history to compress

        turns_to_compress = self._messages[:-self.keep_recent_turns * 2]
        turns_to_keep = self._messages[-self.keep_recent_turns * 2:]

        if not turns_to_compress:
            return

        # Build text of turns to compress:
        history_text = "\n".join(
            f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[complex content]'}"
            for m in turns_to_compress
        )

        existing_summary = f"Previous summary:\n{self._summary}\n\n" if self._summary else ""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Use fast model for compression
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": (
                    f"{existing_summary}"
                    f"Compress these conversation turns into a concise summary that preserves:\n"
                    f"- All decisions made\n"
                    f"- Key facts and constraints established\n"
                    f"- User preferences and requirements stated\n"
                    f"- Current task state and progress\n"
                    f"- Any errors encountered and how they were resolved\n\n"
                    f"Conversation to compress:\n{history_text}\n\n"
                    f"Write a dense summary (max 500 words) that captures everything important."
                )
            }]
        )

        new_summary = response.content[0].text
        self._summary = new_summary
        self._messages = turns_to_keep

        logger.info(
            f"Summarized {len(turns_to_compress)} messages → {estimate_tokens(new_summary)} tokens. "
            f"Utilization: {self._utilization:.0%}"
        )

    def send(self, user_message: str) -> str:
        client = anthropic.Anthropic()

        # Check if we need to summarize before adding more:
        if self._should_summarize():
            logger.warning(f"Context at {self._utilization:.0%} — summarizing old turns")
            self._summarize_old_turns(client)

        self._messages.append({"role": "user", "content": user_message})

        # Build system prompt with summary injected:
        system = self.system_prompt
        if self._summary:
            system += f"\n\n## Conversation Summary (earlier turns)\n{self._summary}"

        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=self._messages
        )

        reply = response.content[0].text
        self._messages.append({"role": "assistant", "content": reply})

        logger.debug(f"Context utilization: {self._utilization:.0%} ({self._used_tokens:,} tokens)")
        return reply

# Usage:
session = ProactivelySummarizingSession(
    system_prompt="You are a software architect assistant helping design a complex system.",
    summarize_threshold=0.70,
    keep_recent_turns=6
)
# Session can run indefinitely without hitting context limit:
for question in ["How should we structure the database?", "What about caching?", "..."]:
    reply = session.send(question)
    print(reply)
```

### Option 2: Rolling summary with checkpoint detection — preserve decisions explicitly

```python
import anthropic
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

@dataclass
class SessionCheckpoint:
    """A structured checkpoint of important decisions and context."""
    decisions: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    task_state: str = ""
    turn_count: int = 0

    def to_prompt_text(self) -> str:
        parts = ["## Session Memory\n"]
        if self.task_state:
            parts.append(f"**Current task state:** {self.task_state}\n")
        if self.decisions:
            parts.append("**Decisions made:**")
            parts.extend(f"- {d}" for d in self.decisions)
        if self.facts:
            parts.append("\n**Established facts:**")
            parts.extend(f"- {f}" for f in self.facts)
        if self.constraints:
            parts.append("\n**Constraints and requirements:**")
            parts.extend(f"- {c}" for c in self.constraints)
        return "\n".join(parts)

def extract_checkpoint(turns: list[dict], existing: Optional[SessionCheckpoint] = None) -> SessionCheckpoint:
    """Extract structured decisions/facts from a batch of conversation turns."""
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in turns
        if isinstance(m.get("content"), str)
    )
    existing_text = existing.to_prompt_text() if existing else ""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"{existing_text}\n\n" if existing_text else ""
                f"Extract the important information from these conversation turns.\n\n"
                f"{history_text}\n\n"
                f"Return JSON with these fields:\n"
                f"{{\"decisions\": [\"list of decisions made\"], "
                f"\"facts\": [\"established facts\"], "
                f"\"constraints\": [\"requirements or constraints\"], "
                f"\"task_state\": \"current task progress in one sentence\"}}"
            )
        }]
    )

    try:
        data = json.loads(response.content[0].text.strip().strip("```json").strip("```"))
        checkpoint = SessionCheckpoint(**{k: v for k, v in data.items() if k in SessionCheckpoint.__dataclass_fields__})
        if existing:
            # Merge with existing checkpoint:
            checkpoint.decisions = list(set(existing.decisions + checkpoint.decisions))[:20]
            checkpoint.facts = list(set(existing.facts + checkpoint.facts))[:20]
            checkpoint.constraints = list(set(existing.constraints + checkpoint.constraints))[:10]
        return checkpoint
    except (json.JSONDecodeError, TypeError):
        return existing or SessionCheckpoint()

class CheckpointSession:
    def __init__(self, model: str = "claude-sonnet-4-6", system: str = ""):
        self._model = model
        self._base_system = system
        self._messages: list[dict] = []
        self._checkpoint: Optional[SessionCheckpoint] = None
        self._turn_count = 0
        self._checkpoint_every = 8  # Create checkpoint every 8 turns

    def send(self, user_message: str) -> str:
        self._messages.append({"role": "user", "content": user_message})
        self._turn_count += 1

        # Create checkpoint at intervals
        if self._turn_count % self._checkpoint_every == 0 and len(self._messages) > 4:
            turns_to_checkpoint = self._messages[:-4]  # Checkpoint all but last 2 pairs
            self._checkpoint = extract_checkpoint(turns_to_checkpoint, self._checkpoint)
            self._messages = self._messages[-4:]  # Keep only last 2 pairs verbatim
            logger.info(f"Checkpoint created at turn {self._turn_count}")

        system = self._base_system
        if self._checkpoint:
            system = self._checkpoint.to_prompt_text() + "\n\n" + system

        response = client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            messages=self._messages
        )
        reply = response.content[0].text
        self._messages.append({"role": "assistant", "content": reply})
        return reply
```

### Option 3: Importance-scored compression — preserve high-value turns

```python
import anthropic
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

@dataclass
class ScoredMessage:
    role: str
    content: str
    importance: float  # 0.0 (disposable) to 1.0 (critical)
    turn_index: int

def score_message_importance(message: dict, turn_index: int) -> float:
    """
    Score a message's importance for retention.
    Higher score = more likely to be kept verbatim.
    """
    content = message.get("content", "")
    if not isinstance(content, str):
        return 0.5  # Default for complex content

    score = 0.0

    # Recency bonus — newer turns are more important:
    score += 0.1  # Base score for all messages

    # Decision markers — turns that establish facts:
    decision_markers = [
        "decided", "agreed", "confirmed", "will use", "we'll", "the plan is",
        "requirement", "constraint", "must", "never", "always", "critical"
    ]
    for marker in decision_markers:
        if marker in content.lower():
            score += 0.15
            break

    # Error markers — turns that resolved issues:
    error_markers = ["error", "bug", "fixed", "resolved", "don't", "avoid", "never do"]
    for marker in error_markers:
        if marker in content.lower():
            score += 0.1
            break

    # Long messages tend to contain more information:
    if len(content) > 500:
        score += 0.1

    return min(1.0, score)

def compress_messages_by_importance(
    messages: list[dict],
    target_token_budget: int,
    min_importance_threshold: float = 0.3
) -> tuple[list[dict], str]:
    """
    Compress message history by:
    1. Scoring each message for importance
    2. Keeping high-importance messages verbatim
    3. Summarizing low-importance messages
    Returns (compressed_messages, summary_of_removed).
    """
    scored = [
        ScoredMessage(
            role=m["role"],
            content=m.get("content", "") if isinstance(m.get("content"), str) else "",
            importance=score_message_importance(m, i),
            turn_index=i
        )
        for i, m in enumerate(messages)
    ]

    # Sort: keep high-importance turns, compress low-importance
    high_importance = [s for s in scored if s.importance >= min_importance_threshold]
    low_importance = [s for s in scored if s.importance < min_importance_threshold]

    # Summarize low-importance turns:
    summary = ""
    if low_importance:
        low_text = "\n".join(f"{s.role}: {s.content[:200]}" for s in low_importance)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Briefly summarize these conversation turns (preserve any decisions or facts):\n\n{low_text}"
            }]
        )
        summary = response.content[0].text

    # Return high-importance messages in original order:
    kept_messages = [
        {"role": s.role, "content": s.content}
        for s in sorted(high_importance, key=lambda x: x.turn_index)
    ]

    return kept_messages, summary
```

### Option 4: Background summarization — async compression during slow API calls

```python
import asyncio
import anthropic
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class AsyncSummarizingSession:
    """
    Async session that triggers summarization in the background
    while waiting for the LLM response — no added latency for the user.
    """
    model: str = "claude-sonnet-4-6"
    _messages: list[dict] = field(default_factory=list)
    _summary: str = ""
    _summarize_task: Optional[asyncio.Task] = None
    _pending_summarize: bool = False

    async def _do_background_summarize(self, turns_to_compress: list[dict]):
        """Run summarization concurrently with the main response."""
        client = anthropic.AsyncAnthropic()
        history_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in turns_to_compress
            if isinstance(m.get("content"), str)
        )
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Summarize these conversation turns, preserving all decisions and key facts:\n\n"
                        f"{history_text}"
                    )
                }]
            )
            self._summary = response.content[0].text
            logger.info(f"Background summary complete: {len(self._summary)} chars")
        except Exception as exc:
            logger.warning(f"Background summarization failed: {exc}")

    async def send(self, user_message: str) -> str:
        client = anthropic.AsyncAnthropic()

        # Wait for any pending background summarization to complete:
        if self._summarize_task and not self._summarize_task.done():
            await self._summarize_task

        # Trim summarized turns from messages:
        if self._pending_summarize:
            self._messages = self._messages[-8:]  # Keep last 4 pairs
            self._pending_summarize = False

        self._messages.append({"role": "user", "content": user_message})

        system = "You are a helpful assistant."
        if self._summary:
            system += f"\n\n## Earlier conversation summary:\n{self._summary}"

        # Start main response and (if needed) background summarization simultaneously:
        tasks = [
            client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=self._messages
            )
        ]

        # Trigger background summarization if history is getting long:
        if len(self._messages) > 20 and not self._pending_summarize:
            turns_to_compress = self._messages[:-8]
            self._summarize_task = asyncio.create_task(
                self._do_background_summarize(turns_to_compress)
            )
            self._pending_summarize = True
            logger.info("Started background summarization")

        response = await tasks[0]
        reply = response.content[0].text
        self._messages.append({"role": "assistant", "content": reply})
        return reply
```

### Option 5: Sliding window with pinned messages — always keep critical turns

```python
import anthropic
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class SlidingWindowSession:
    """
    Sliding window over conversation history with "pinned" messages
    that are always included regardless of age.
    """
    model: str = "claude-sonnet-4-6"
    window_size: int = 20           # Keep last N messages in sliding window
    system_prompt: str = ""
    _messages: list[dict] = field(default_factory=list)
    _pinned: list[dict] = field(default_factory=list)  # Always included

    def pin_message(self, message: dict):
        """Pin a message so it's always included regardless of window position."""
        self._pinned.append(message)
        logger.info(f"Pinned message: {str(message.get('content', ''))[:50]}...")

    def _get_effective_messages(self) -> list[dict]:
        """Combine pinned messages with the sliding window."""
        window = self._messages[-self.window_size:]
        # Interleave: pinned messages first, then recent window
        # Remove from window any turns already covered by pinned:
        effective = self._pinned + [m for m in window if m not in self._pinned]
        return effective

    def send(self, user_message: str, pin_this_turn: bool = False) -> str:
        client = anthropic.Anthropic()

        user_msg = {"role": "user", "content": user_message}
        self._messages.append(user_msg)

        if pin_this_turn:
            self.pin_message(user_msg)

        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=self.system_prompt,
            messages=self._get_effective_messages()
        )

        reply = response.content[0].text
        assistant_msg = {"role": "assistant", "content": reply}
        self._messages.append(assistant_msg)

        # Auto-detect important assistant responses to pin:
        importance_markers = ["I'll remember", "We decided", "The constraint is", "Important:"]
        if any(m in reply for m in importance_markers):
            self.pin_message(assistant_msg)
            logger.info("Auto-pinned important assistant response")

        return reply

# Usage — pin the initial requirements so they're never dropped:
session = SlidingWindowSession(window_size=16, system_prompt="You are a software architect.")
session.send("The system must support 10,000 concurrent users and use PostgreSQL.", pin_this_turn=True)
session.send("Let's start with the authentication service.")
# ... 50 more turns ...
# The requirements turn is always in context even after 50 turns
```

### Option 6: Summarization quality check — verify the summary before replacing turns

```python
import anthropic
import json
import logging

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def summarize_and_verify(
    turns_to_compress: list[dict],
    model: str = "claude-sonnet-4-6"
) -> tuple[str, bool]:
    """
    Summarize turns and verify the summary is faithful.
    Returns (summary, is_verified).
    If verification fails, returns the raw first 2,000 chars as fallback.
    """
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in turns_to_compress
        if isinstance(m.get("content"), str)
    )[:10_000]  # Limit input to avoid token explosion

    # Step 1: Generate summary
    summary_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                "Summarize these conversation turns. Preserve: all decisions, key facts, "
                "user requirements, error resolutions, and current task state.\n\n"
                + history_text
            )
        }]
    )
    summary = summary_response.content[0].text

    # Step 2: Verify key information is preserved
    verify_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Original conversation:\n{history_text[:3000]}\n\n"
                f"Summary:\n{summary}\n\n"
                "Does the summary preserve all important decisions, requirements, and facts from the original? "
                "Reply with JSON: {\"verified\": true/false, \"missing\": [\"list of missing important items\"]}"
            )
        }]
    )

    try:
        verification = json.loads(verify_response.content[0].text.strip().strip("```json").strip("```"))
        verified = verification.get("verified", False)
        missing = verification.get("missing", [])

        if not verified and missing:
            logger.warning(f"Summary missing: {missing}")
            # Append missing items to summary:
            summary += f"\n\nAdditional important context:\n" + "\n".join(f"- {m}" for m in missing)

        return summary, True
    except json.JSONDecodeError:
        return summary, False
```

## Compression Strategy Comparison

| Strategy | Memory Preservation | Latency | Best For |
|---|---|---|---|
| Proactive threshold summarization (Option 1) | Good | Slight pause at threshold | Most agents |
| Checkpoint with structured decisions (Option 2) | Excellent | Periodic pause | Decision-heavy sessions |
| Importance-scored compression (Option 3) | Very good | Pause at compression | Research sessions |
| Background async summarization (Option 4) | Good | Zero added latency | Async agents |
| Sliding window + pinned messages (Option 5) | Selective | None | When key turns are known |
| Verified summarization (Option 6) | Excellent | Higher pause | High-stakes tasks |

## Expected Token Savings
200K context window, no compression: agent fails at turn ~50 (depends on message length)
With proactive summarization at 70%: agent runs indefinitely at ~140K tokens steady state
Summary token cost (Haiku): ~500 tokens/compression × $0.00025/1K = $0.000125 per compression
That's essentially free compared to the cost of failing a long-running task

## Environment
- Any agent handling extended multi-turn interactions: coding sessions, research agents, project planning assistants, customer support bots; most critical when the task is too complex to complete in fewer than 15-20 turns — implement summarization as the default, not as a fix for hitting the limit; hitting the context window limit is always a bug, never a feature
- Source: direct experience; context window exhaustion mid-task is the third most common production failure for autonomous agents (after OOM and SIGTERM), and it always happens at the worst possible moment — usually just before the agent was about to complete the task
