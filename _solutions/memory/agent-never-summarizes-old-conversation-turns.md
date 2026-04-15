---
layout: solution
title: "Agent Never Summarizes Old Conversation Turns"
category: memory
description: "Every new message appends the full history, causing the context window to fill and old messages to be truncated without warning."
tags: [memory, context-window, summarization, token-cost, conversation-management]
---

## Symptom

Multi-turn sessions start failing after 20–30 exchanges. Early messages are silently dropped when the context limit is reached, so the agent forgets decisions made at the start of the conversation. Alternatively, each request costs thousands of tokens replaying conversation history that could have been compressed hours ago.

## Root Cause

A raw append-only history grows linearly with every turn. Each message retains full verbosity: pleasantries, tool call traces, intermediate reasoning steps, and error messages that are no longer relevant. Most of this content is retrievable from a short summary. Without periodic summarization the agent either hits the context limit or pays to re-read irrelevant detail on every call.

## Fix

### Option 1 — Summarize when history exceeds a token threshold

```python
import anthropic

client = anthropic.Anthropic()

MAX_HISTORY_TOKENS = 4000
SUMMARY_MODEL = "claude-haiku-4-5-20251001"

def count_tokens(messages: list[dict]) -> int:
    """Rough estimate: 4 chars ≈ 1 token."""
    return sum(len(str(m.get("content", ""))) // 4 for m in messages)

def summarize_history(messages: list[dict]) -> str:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages
        if isinstance(m.get("content"), str)
    )
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "Summarise the following conversation in 200 words or fewer. "
                "Preserve: decisions made, facts established, open questions, user preferences.\n\n"
                + transcript
            ),
        }],
    )
    return response.content[0].text

class SummarizingHistory:
    def __init__(self):
        self._messages: list[dict] = []
        self._summary:  str        = ""

    def add(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})
        if count_tokens(self._messages) > MAX_HISTORY_TOKENS:
            self._compress()

    def _compress(self) -> None:
        # Keep the last 4 turns verbatim for immediate context
        keep = self._messages[-4:]
        old  = self._messages[:-4]
        new_summary = summarize_history(old)
        self._summary = (self._summary + "\n\n" + new_summary).strip() if self._summary else new_summary
        self._messages = keep
        print(f"[memory] compressed {len(old)} messages → summary ({len(new_summary)} chars)")

    def as_api_messages(self) -> list[dict]:
        if not self._summary:
            return self._messages
        # Inject summary as a system-style user turn at the top
        return [
            {"role": "user",      "content": f"[Conversation summary so far]\n{self._summary}"},
            {"role": "assistant", "content": "Understood. I have the context from our earlier conversation."},
            *self._messages,
        ]


history = SummarizingHistory()

def chat(user_input: str) -> str:
    history.add("user", user_input)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=history.as_api_messages(),
    )
    reply = response.content[0].text
    history.add("assistant", reply)
    return reply

for msg in [
    "My name is Alex. I'm building a recipe recommendation app.",
    "The tech stack is FastAPI + PostgreSQL.",
    "I want to store user dietary restrictions.",
    "Let's model this as a many-to-many relationship.",
    "Now, how should I design the API endpoint for filtering recipes?",
]:
    print(f"User: {msg}")
    print(f"Agent: {chat(msg)}\n")
```

**Expected Token Savings:** 60–80% reduction in history tokens after first compression; savings grow with session length.
**Environment:** Multi-turn chatbots and long coding sessions; any agent where session length exceeds 20 turns.

---

### Option 2 — Rolling summary updated after every N turns

```python
import anthropic

client = anthropic.Anthropic()

SUMMARIZE_EVERY_N = 6   # compress after every 6 new turns

def update_rolling_summary(existing_summary: str, new_turns: list[dict]) -> str:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in new_turns
        if isinstance(m.get("content"), str)
    )
    prompt = (
        f"Existing summary:\n{existing_summary}\n\n"
        f"New conversation turns:\n{transcript}\n\n"
        "Update the summary to include the new information. Keep it under 300 words. "
        "Preserve all decisions, user preferences, and open tasks."
    ) if existing_summary else (
        f"Summarise this conversation in under 300 words:\n{transcript}"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


class RollingSummaryChat:
    def __init__(self):
        self.summary       = ""
        self.recent_turns  : list[dict] = []
        self.turn_count    = 0

    def add(self, role: str, content: str) -> None:
        self.recent_turns.append({"role": role, "content": content})
        self.turn_count += 1
        if self.turn_count % SUMMARIZE_EVERY_N == 0:
            self.summary = update_rolling_summary(self.summary, self.recent_turns)
            self.recent_turns = []  # clear compressed turns
            print(f"[memory] rolling summary updated at turn {self.turn_count}")

    def build_messages(self, new_user_message: str) -> list[dict]:
        msgs = []
        if self.summary:
            msgs += [
                {"role": "user",      "content": f"[Summary]\n{self.summary}"},
                {"role": "assistant", "content": "Noted."},
            ]
        msgs += self.recent_turns
        msgs.append({"role": "user", "content": new_user_message})
        return msgs

    def chat(self, user_message: str) -> str:
        messages = self.build_messages(user_message)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=messages,
        )
        reply = response.content[0].text
        self.add("user", user_message)
        self.add("assistant", reply)
        return reply


session = RollingSummaryChat()
print(session.chat("I'm designing a REST API for a library management system."))
print(session.chat("Books have authors, genres, and ISBNs."))
print(session.chat("Users can borrow up to 5 books at a time."))
print(session.chat("Let's add a reservation queue for popular books."))
print(session.chat("How should overdue notifications work?"))
print(session.chat("What database indexes should I create?"))
print(session.chat("Now design the checkout endpoint."))  # triggers summarization
```

**Expected Token Savings:** Fixed-size compression window keeps history constant regardless of session length.
**Environment:** Long-running sessions (e.g., multi-hour coding assistants, game masters, research agents).

---

### Option 3 — Hierarchical summarization (episode → session → project)

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

EPISODE_TURNS = 8    # group turns into episodes
MAX_EPISODES  = 5    # keep up to 5 episode summaries before creating a session summary

def summarize(text: str, instruction: str, max_tokens: int = 256) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": f"{instruction}\n\n{text}"}],
    )
    return response.content[0].text

@dataclass
class HierarchicalMemory:
    current_turns:    list[dict] = field(default_factory=list)
    episode_summaries: list[str] = field(default_factory=list)
    session_summary:  str        = ""

    def add_turn(self, role: str, content: str) -> None:
        self.current_turns.append({"role": role, "content": content})

        if len(self.current_turns) >= EPISODE_TURNS * 2:  # *2 for user+assistant
            self._close_episode()

    def _close_episode(self) -> None:
        transcript = "\n".join(
            f"{m['role'].upper()}: {m['content'][:200]}" for m in self.current_turns
            if isinstance(m.get("content"), str)
        )
        ep_summary = summarize(transcript, "Summarise this episode in 100 words.")
        self.episode_summaries.append(ep_summary)
        self.current_turns = []
        print(f"[memory] episode closed → {len(self.episode_summaries)} episodes stored")

        if len(self.episode_summaries) >= MAX_EPISODES:
            self._close_session()

    def _close_session(self) -> None:
        combined = "\n\n".join(f"Episode {i+1}: {s}" for i, s in enumerate(self.episode_summaries))
        self.session_summary = summarize(combined, "Merge these episode summaries into one coherent 200-word session summary.", max_tokens=300)
        self.episode_summaries = []
        print("[memory] session summary created from episodes")

    def context_messages(self) -> list[dict]:
        parts = []
        if self.session_summary:
            parts.append(f"[Session memory]\n{self.session_summary}")
        if self.episode_summaries:
            parts.append("[Recent episodes]\n" + "\n\n".join(self.episode_summaries))
        if not parts:
            return self.current_turns
        prefix = "\n\n".join(parts)
        return [
            {"role": "user",      "content": prefix},
            {"role": "assistant", "content": "I have the context from earlier."},
            *self.current_turns,
        ]

memory = HierarchicalMemory()

def chat(msg: str) -> str:
    messages = memory.context_messages() + [{"role": "user", "content": msg}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=messages,
    )
    reply = response.content[0].text
    memory.add_turn("user", msg)
    memory.add_turn("assistant", reply)
    return reply

for m in ["Starting a new project.", "It's a trading platform.", "We need real-time prices.",
          "PostgreSQL for trades.", "Redis for caching.", "WebSocket for streaming.",
          "Auth via JWT.", "Two-factor for large trades."]:
    print(f"User: {m}\nAgent: {chat(m)}\n")
```

**Expected Token Savings:** 80–90% on long sessions; three-tier compression keeps context window usage nearly constant regardless of total session length.
**Environment:** Very long sessions (hours, days) — coding agents, research assistants, game masters.

---

### Option 4 — Entity-preserving summary: extract facts before compressing

```python
import json
import anthropic

client = anthropic.Anthropic()

EXTRACT_SYSTEM = """Extract key facts from the conversation as a JSON object with these fields:
{
  "decisions": ["list of decisions made"],
  "entities":  {"name": "description"},
  "open_tasks": ["list of unresolved tasks"],
  "preferences": ["user preferences mentioned"]
}
Only include non-empty arrays/objects."""

def extract_facts(messages: list[dict]) -> dict:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages
        if isinstance(m.get("content"), str)
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": transcript}],
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {}

def facts_to_prompt(facts: dict) -> str:
    lines = ["[Extracted memory]"]
    if facts.get("decisions"):
        lines.append("Decisions: " + "; ".join(facts["decisions"]))
    if facts.get("entities"):
        lines.append("Entities: " + "; ".join(f"{k}={v}" for k, v in facts["entities"].items()))
    if facts.get("open_tasks"):
        lines.append("Open tasks: " + "; ".join(facts["open_tasks"]))
    if facts.get("preferences"):
        lines.append("Preferences: " + "; ".join(facts["preferences"]))
    return "\n".join(lines)

# Simulate compressing a 10-turn history
sample_history = [
    {"role": "user",      "content": "I'm Alice, building a SaaS analytics dashboard."},
    {"role": "assistant", "content": "Great! What's the primary user persona?"},
    {"role": "user",      "content": "Marketing managers. They need funnel visualisations."},
    {"role": "assistant", "content": "Understood. We should use D3.js for charts."},
    {"role": "user",      "content": "Agreed. Let's use React for the frontend."},
    {"role": "assistant", "content": "And FastAPI for the backend?"},
    {"role": "user",      "content": "Yes. We also need multi-tenancy from day one."},
    {"role": "assistant", "content": "I'll design the schema with tenant_id on every table."},
    {"role": "user",      "content": "Good. The MVP needs to ship by end of month."},
    {"role": "assistant", "content": "Noted. Let's prioritise the funnel chart first."},
]

facts   = extract_facts(sample_history)
summary = facts_to_prompt(facts)
print(summary)

# Continue with compact memory instead of full history
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[
        {"role": "user",      "content": summary},
        {"role": "assistant", "content": "I have the context."},
        {"role": "user",      "content": "Now design the tenant isolation middleware."},
    ],
)
print(response.content[0].text)
```

**Expected Token Savings:** Structured fact extraction produces much denser summaries than prose; typically 90%+ compression of raw history.
**Environment:** Agents that need to remember specific named entities, decisions, and tasks — project planners, coding assistants.

---

### Option 5 — Async background summarizer (non-blocking)

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def summarize_async(messages: list[dict]) -> str:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}" for m in messages
        if isinstance(m.get("content"), str)
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"Summarise in 150 words:\n\n{transcript}",
        }],
    )
    return response.content[0].text


class AsyncSummarizingHistory:
    def __init__(self, compress_every: int = 8):
        self._turns:        list[dict]              = []
        self._summary:      str                     = ""
        self._compress_every = compress_every
        self._summary_task: asyncio.Task | None     = None

    def add(self, role: str, content: str) -> None:
        self._turns.append({"role": role, "content": content})
        if len(self._turns) >= self._compress_every * 2 and self._summary_task is None:
            # Fire off summarization in the background — don't block the next user turn
            to_compress     = self._turns[:-4]
            self._turns     = self._turns[-4:]
            self._summary_task = asyncio.ensure_future(self._do_compress(to_compress))

    async def _do_compress(self, turns: list[dict]) -> None:
        new_summary = await summarize_async(turns)
        self._summary = (self._summary + "\n\n" + new_summary).strip() if self._summary else new_summary
        self._summary_task = None
        print(f"[memory] background summarization complete ({len(new_summary)} chars)")

    def messages(self, user_input: str) -> list[dict]:
        base = []
        if self._summary:
            base = [
                {"role": "user",      "content": f"[Summary]\n{self._summary}"},
                {"role": "assistant", "content": "Noted."},
            ]
        return base + self._turns + [{"role": "user", "content": user_input}]


history = AsyncSummarizingHistory(compress_every=4)

async def chat(msg: str) -> str:
    messages = history.messages(msg)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=messages,
    )
    reply = response.content[0].text
    history.add("user", msg)
    history.add("assistant", reply)
    return reply

async def main():
    for m in ["Hi, I'm working on a CLI tool.", "It needs a plugin system.", "Plugins load from ~/.myapp/plugins/.",
              "Each plugin is a Python module.", "Plugins expose a run() function.", "Add error isolation per plugin.",
              "Now, how do I hot-reload plugins?"]:
        print(f"User: {m}")
        print(f"Agent: {await chat(m)}\n")

asyncio.run(main())
```

**Expected Token Savings:** Summarization runs concurrently with the next user turn; zero added latency to the response.
**Environment:** Low-latency chat agents where blocking on summarization would degrade the user experience.

---

### Option 6 — Importance-scored pruning (keep high-signal turns verbatim)

```python
import json
import anthropic

client = anthropic.Anthropic()

SCORE_SYSTEM = """Rate each conversation turn for importance on a 1–5 scale.
5 = decision made, fact established, error resolved
3 = relevant context, tool result
1 = pleasantry, filler, repeated info
Respond with JSON: {"scores": [1, 5, 3, ...]} — one score per message in order."""

def score_turns(messages: list[dict]) -> list[int]:
    content = json.dumps([{"role": m["role"], "content": str(m.get("content", ""))[:200]}
                          for m in messages])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SCORE_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    try:
        return json.loads(response.content[0].text)["scores"]
    except (json.JSONDecodeError, KeyError):
        return [3] * len(messages)

def prune_history(messages: list[dict], keep_recent: int = 4, score_threshold: int = 4) -> list[dict]:
    if len(messages) <= keep_recent:
        return messages
    older   = messages[:-keep_recent]
    recent  = messages[-keep_recent:]
    scores  = score_turns(older)
    kept    = [m for m, s in zip(older, scores) if s >= score_threshold]
    dropped = len(older) - len(kept)
    print(f"[memory] pruned {dropped}/{len(older)} low-importance turns")
    return kept + recent

# Build a sample history
history = [
    {"role": "user",      "content": "Hi there!"},
    {"role": "assistant", "content": "Hello! How can I help?"},
    {"role": "user",      "content": "We decided to use PostgreSQL for the main store."},
    {"role": "assistant", "content": "Great choice. I'll use PostgreSQL throughout."},
    {"role": "user",      "content": "Cool. So anyway, nice weather today haha"},
    {"role": "assistant", "content": "Ha! Indeed. Back to the project?"},
    {"role": "user",      "content": "The auth service must use JWT with RS256."},
    {"role": "assistant", "content": "Noted — JWT with RS256 for auth."},
]

pruned = prune_history(history, keep_recent=2, score_threshold=4)
print(f"Kept {len(pruned)} of {len(history)} messages:")
for m in pruned:
    print(f"  {m['role']}: {m['content'][:80]}")
```

**Expected Token Savings:** Discards low-signal turns (pleasantries, filler) while keeping high-signal turns verbatim; 40–70% reduction depending on conversation quality.
**Environment:** Conversations with mixed signal quality; combines well with summarization for maximum compression.

---

## Comparison

| Option | Trigger | Compression | Preserves Detail | Latency Cost | Best For |
|---|---|---|---|---|---|
| 1. Token threshold | Token count | Prose summary | Medium | Synchronous | Simple threshold-based pruning |
| 2. Rolling N turns | Turn count | Incremental summary | Medium | Synchronous | Predictable compression cadence |
| 3. Hierarchical | Turn + episode count | 3-tier summary | Low | Synchronous | Very long sessions (hours/days) |
| 4. Entity extraction | Manual trigger | Structured JSON | High | Synchronous | Agents needing precise fact recall |
| 5. Async background | Turn count | Prose summary | Medium | Zero (async) | Low-latency chat requiring no delay |
| 6. Importance scoring | Manual trigger | Selective prune | High | Synchronous | Mixed-signal conversations |
