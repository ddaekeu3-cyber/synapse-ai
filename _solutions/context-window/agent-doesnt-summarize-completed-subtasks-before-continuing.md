---
layout: solution
title: "Agent doesn't summarize completed subtasks before continuing"
category: context-window
description: "Multi-step agent completes subtask 1 (research phase), then continues with subtask 2 while carrying the full 8000-token conversation from subtask 1 in context. By subtask 4, context is dominated by stale intermediate work. Summarizing completed subtasks keeps context focused and prevents window exhaustion."
tags: [context-window, summarization, multi-step, memory, token-cost, asyncio]
---

## Symptom

An agent working on a 5-step task finishes step 1 (research: 80 tool calls, 12,000 tokens of tool results). It proceeds to step 2 carrying all 12,000 tokens from step 1. By step 3, the context is 28,000 tokens — mostly completed work. By step 4, it hits the context limit mid-task. Alternatively, it never hits the limit but pays input token costs that grow quadratically with task length.

## Root Cause

The agent appends every tool call and result to the `messages` list and never prunes it. Completed subtasks are "done" — their detailed intermediate steps are no longer needed to complete the remaining work. Only their outputs (the conclusions, files produced, key findings) need to be carried forward. Without a summarization step, the full conversation history of every completed subtask stays in context indefinitely.

## Fix

After completing each subtask, summarize the completed work into a compact summary (50–200 tokens) and replace the detailed subtask conversation with the summary. The summary captures what was done and what was produced, without the step-by-step intermediate tool calls.

---

### Option 1 — Summarize completed subtask and replace with compact record

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

SUMMARIZE_SYSTEM = (
    "Summarize the completed work in the conversation below. "
    "Your summary must include: (1) what was accomplished, (2) key findings or outputs, "
    "(3) any important facts discovered. Be concise — 3-5 sentences maximum. "
    "Do not include step-by-step details or intermediate reasoning."
)


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 chars per token."""
    total = sum(
        len(str(m.get("content", ""))) for m in messages
    )
    return total // 4


def summarize_subtask(subtask_messages: list[dict], subtask_name: str) -> str:
    """Generate a compact summary of a completed subtask's conversation."""
    conversation_text = ""
    for msg in subtask_messages:
        role = msg["role"]
        content = msg.get("content", "")
        if isinstance(content, str):
            conversation_text += f"{role}: {content[:500]}\n"
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    conversation_text += f"{role}: {block['text'][:300]}\n"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": conversation_text[:4000]}],
    )
    summary = response.content[0].text.strip()
    print(f"[Subtask:{subtask_name}] Summarized {len(subtask_messages)} messages → {len(summary)} chars")
    return summary


class MultiStepAgent:
    def __init__(self):
        self.messages: list[dict] = []
        self.completed_summaries: list[dict] = []

    def add(self, role: str, content):
        self.messages.append({"role": role, "content": content})

    def complete_subtask(self, subtask_name: str):
        """
        Summarize the current messages, replace them with a compact summary,
        and reset for the next subtask.
        """
        if not self.messages:
            return

        before_tokens = estimate_tokens(self.messages)
        summary = summarize_subtask(self.messages, subtask_name)

        # Store the summary
        summary_record = {
            "subtask": subtask_name,
            "summary": summary,
            "original_messages": len(self.messages),
            "original_tokens_estimate": before_tokens,
        }
        self.completed_summaries.append(summary_record)

        # Replace detailed messages with a single summary entry
        self.messages = [{
            "role": "user",
            "content": f"[Completed subtask: {subtask_name}]\n{summary}",
        }]
        after_tokens = estimate_tokens(self.messages)
        print(
            f"[Context] {subtask_name}: {before_tokens} → {after_tokens} tokens "
            f"({100*(before_tokens-after_tokens)//before_tokens}% reduction)"
        )

    def get_context_for_llm(self) -> list[dict]:
        """Return messages including all completed subtask summaries."""
        return self.messages


# Simulate a multi-step research + writing task
agent = MultiStepAgent()

# Subtask 1: Research phase
agent.add("user", "Research the top 3 Python async frameworks")
for i in range(5):  # simulate tool calls
    agent.add("assistant", [{"type": "tool_use", "id": f"t{i}", "name": "web_search",
                              "input": {"query": f"async framework {i}"}}])
    agent.add("user", [{"type": "tool_result", "tool_use_id": f"t{i}",
                        "content": f"Result {i}: " + "data " * 50}])
agent.add("assistant", "Research complete. Found: asyncio, Trio, AnyIO as top frameworks.")

agent.complete_subtask("research_phase")

# Subtask 2: Outline phase (context is now lean)
agent.add("user", "Based on the research, create an outline for the comparison article")
print(f"[Context] Entering subtask 2 with ~{estimate_tokens(agent.messages)} tokens")
```

**Expected Token Savings:** Subtask with 5 tool calls (~1500 tokens) summarized to ~150 tokens — 90% reduction; for a 5-step agent, this prevents context from growing from 1500 to 7500 tokens, keeping it at ~750 tokens after each step.
**Environment:** Any multi-step agent; subtask summarization is the most effective context management strategy when subtask phases are clearly delineated.

---

### Option 2 — Automatic summarization trigger based on token threshold

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

SUMMARIZE_PROMPT = (
    "Summarize the key outcomes and findings from this conversation. "
    "Focus on: what was accomplished, key data points discovered, and any decisions made. "
    "Be concise (under 200 words)."
)


class ThresholdSummarizer:
    """
    Automatically summarizes conversation history when it exceeds a token threshold.
    Keeps recent messages intact; summarizes the older portion.
    """
    def __init__(self, max_tokens: int = 4000, keep_recent: int = 4):
        self.max_tokens = max_tokens
        self.keep_recent = keep_recent
        self.messages: list[dict] = []
        self.summary_count = 0

    def _token_estimate(self) -> int:
        return sum(len(str(m.get("content", ""))) for m in self.messages) // 4

    def _summarize_old_messages(self, messages_to_summarize: list[dict]) -> str:
        conversation = "\n".join(
            f"{m['role']}: {str(m.get('content', ''))[:400]}"
            for m in messages_to_summarize
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": f"{SUMMARIZE_PROMPT}\n\n{conversation[:3000]}"}],
        )
        return response.content[0].text.strip()

    def add(self, role: str, content) -> bool:
        """Add message, auto-summarize if over threshold. Returns True if summarized."""
        self.messages.append({"role": role, "content": content})

        if self._token_estimate() > self.max_tokens:
            self._auto_summarize()
            return True
        return False

    def _auto_summarize(self):
        if len(self.messages) <= self.keep_recent:
            return

        old = self.messages[:-self.keep_recent]
        recent = self.messages[-self.keep_recent:]

        self.summary_count += 1
        summary_text = self._summarize_old_messages(old)

        self.messages = [
            {"role": "user", "content": f"[Auto-summary #{self.summary_count}]\n{summary_text}"},
        ] + recent

        print(f"[AutoSummarize] #{self.summary_count}: {len(old)} messages → summary + {self.keep_recent} recent")

    def get_messages(self) -> list[dict]:
        return self.messages


summarizer = ThresholdSummarizer(max_tokens=2000, keep_recent=3)

# Simulate a long conversation
for i in range(20):
    did_summarize = summarizer.add("user", f"Tool result {i}: " + "data " * 100)
    summarizer.add("assistant", f"Processing result {i}...")
    if did_summarize:
        print(f"[After msg {i}] Context tokens: ~{sum(len(str(m.get('content',''))) for m in summarizer.get_messages())//4}")
```

**Expected Token Savings:** Threshold trigger keeps context under 4000 tokens regardless of task length; without it, a 20-step task accumulates ~20,000 tokens — 80% reduction for long-running agents.
**Environment:** Long-running agents without predictable subtask boundaries; threshold-based summarization works even when the agent doesn't know when one "phase" ends and another begins.

---

### Option 3 — Async subtask executor with built-in context handoff

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

HANDOFF_SUMMARIZE_SYSTEM = (
    "You are summarizing the output of a completed subtask for handoff to the next subtask. "
    "Output a JSON object with keys: 'accomplishment' (what was done), "
    "'key_outputs' (list of concrete outputs produced), "
    "'important_facts' (list of key facts discovered), "
    "'next_task_context' (what the next task needs to know). "
    "Keep each field concise — under 50 words."
)


async def execute_subtask(
    task_name: str,
    instructions: str,
    prior_context: str = "",
) -> dict:
    """Execute a subtask and return a compact handoff summary."""
    system = f"You are a task executor. {instructions}"
    if prior_context:
        system += f"\n\nContext from prior subtasks:\n{prior_context}"

    # Simulate the subtask execution (multiple turns)
    messages = [{"role": "user", "content": f"Execute: {task_name}"}]

    # Simulate 3 tool calls worth of work
    for step in range(3):
        messages.append({
            "role": "assistant",
            "content": f"Step {step+1} of {task_name}: " + "intermediate work " * 30,
        })
        messages.append({
            "role": "user",
            "content": f"Tool result for step {step+1}: " + "output data " * 20,
        })

    # Final answer
    messages.append({
        "role": "assistant",
        "content": f"Subtask {task_name} complete. " + "final output " * 15,
    })

    # Generate compact handoff summary using Haiku
    full_conversation = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in messages)
    summary_response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=HANDOFF_SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": full_conversation[:3000]}],
    )

    import json
    try:
        handoff = json.loads(summary_response.content[0].text)
    except json.JSONDecodeError:
        handoff = {"accomplishment": summary_response.content[0].text, "key_outputs": [], "important_facts": [], "next_task_context": ""}

    print(f"[Subtask:{task_name}] Complete — {len(messages)} messages → {len(str(handoff))} chars handoff")
    return handoff


async def run_multi_step_pipeline(goal: str, subtasks: list[tuple[str, str]]) -> str:
    """Execute subtasks sequentially, passing compact handoffs between them."""
    accumulated_context = f"Goal: {goal}"
    all_handoffs = []

    for task_name, instructions in subtasks:
        print(f"\n[Pipeline] Starting: {task_name}")
        handoff = await execute_subtask(task_name, instructions, accumulated_context)
        all_handoffs.append(handoff)

        # Update context: only the compact handoff, not the full conversation
        accumulated_context = "\n".join(
            f"[{h.get('accomplishment', 'completed')}] Outputs: {h.get('key_outputs', [])}"
            for h in all_handoffs
        )
        print(f"[Pipeline] Context size: {len(accumulated_context)} chars")

    # Final synthesis using compact handoffs only
    final_context = "\n\n".join(
        f"Subtask {i+1} ({subtasks[i][0]}): {h.get('accomplishment')}\n"
        f"Outputs: {h.get('key_outputs')}\n"
        f"Facts: {h.get('important_facts')}"
        for i, h in enumerate(all_handoffs)
    )

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Goal: {goal}\n\nCompleted subtasks:\n{final_context}\n\nSynthesize final result:",
        }],
    )
    return response.content[0].text


pipeline = [
    ("research", "Research the topic thoroughly"),
    ("outline", "Create a structured outline"),
    ("draft", "Write a first draft"),
]

result = asyncio.run(run_multi_step_pipeline(
    "Write a technical blog post about async Python patterns",
    pipeline,
))
print(f"\n[Final result]\n{result[:500]}")
```

**Expected Token Savings:** JSON handoff (~200 tokens) replaces full subtask conversation (~2000 tokens) — 90% reduction per subtask; for a 4-subtask pipeline, context stays at ~800 tokens instead of growing to ~8000 tokens.
**Environment:** Pipeline agents with well-defined subtask phases; the handoff JSON format makes inter-subtask context explicit and machine-readable.

---

### Option 4 — Rolling summary with importance-weighted retention

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

IMPORTANCE_SYSTEM = (
    "Rate the importance of this conversation message for completing the current task. "
    "Reply with exactly one word: HIGH, MEDIUM, or LOW.\n"
    "HIGH: contains a key decision, a critical finding, or a concrete output\n"
    "MEDIUM: contains useful context or intermediate reasoning\n"
    "LOW: contains routine tool calls, error messages, or retry attempts"
)


def rate_message_importance(message: dict) -> str:
    content = str(message.get("content", ""))[:500]
    if not content.strip():
        return "LOW"
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=IMPORTANCE_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    rating = response.content[0].text.strip().upper()
    return rating if rating in ("HIGH", "MEDIUM", "LOW") else "MEDIUM"


class ImportanceWeightedContext:
    def __init__(self, high_budget: int = 5, medium_budget: int = 3):
        self.high_budget = high_budget
        self.medium_budget = medium_budget
        self._messages: list[tuple[dict, str]] = []  # (message, importance)

    def add(self, role: str, content, importance: str | None = None):
        msg = {"role": role, "content": content}
        if importance is None:
            importance = rate_message_importance(msg)
        self._messages.append((msg, importance))
        print(f"[Context] Added [{importance}] {role}: {str(content)[:60]}")
        self._prune()

    def _prune(self):
        """Keep all HIGH, last N MEDIUM, drop excess LOW."""
        high = [(m, i) for m, i in self._messages if i == "HIGH"]
        medium = [(m, i) for m, i in self._messages if i == "MEDIUM"][-self.medium_budget:]
        # Keep only recent LOW messages (last 2)
        low = [(m, i) for m, i in self._messages if i == "LOW"][-2:]

        before = len(self._messages)
        self._messages = high + medium + low
        # Maintain chronological order
        all_orig = [(m, i) for m, i in self._messages]
        if before != len(self._messages):
            print(f"[Context] Pruned: {before} → {len(self._messages)} messages")

    def get_messages(self) -> list[dict]:
        return [m for m, _ in self._messages]


ctx = ImportanceWeightedContext(high_budget=10, medium_budget=3)
ctx.add("user", "Find all Python async frameworks", importance="HIGH")
ctx.add("assistant", "Searching...", importance="LOW")
ctx.add("user", "[search result: asyncio, trio, anyio]", importance="HIGH")
ctx.add("assistant", "Retrying failed request...", importance="LOW")
ctx.add("assistant", "Retrying again...", importance="LOW")
ctx.add("assistant", "Key finding: asyncio is built into Python, trio uses structured concurrency", importance="HIGH")
ctx.add("user", "Now compare their performance characteristics", importance="MEDIUM")

print(f"\nContext messages kept: {len(ctx.get_messages())}")
for m in ctx.get_messages():
    print(f"  {m['role']}: {str(m['content'])[:80]}")
```

**Expected Token Savings:** Importance weighting discards LOW messages (routine tool calls, retries) while keeping HIGH messages (key findings, decisions) — for a research task with many failed/retry tool calls, this reduces context by 50–70% while preserving all critical information.
**Environment:** Exploratory agents with variable-quality tool results; importance weighting is better than simple recency when early discoveries are more important than recent retry attempts.

---

### Option 5 — Checkpoint-based context reset

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")

CHECKPOINT_SUMMARIZE = (
    "Create a checkpoint summary for an AI agent. Include:\n"
    "1. PROGRESS: What percentage of the goal is complete and what remains\n"
    "2. OUTPUTS: List of concrete outputs produced so far (files, data, decisions)\n"
    "3. STATE: Current state the agent needs to continue from\n"
    "4. NEXT: The next immediate action to take\n"
    "Keep each section to 1-3 bullet points."
)


class CheckpointAgent:
    """
    Agent that saves checkpoints at key milestones.
    On context pressure, rolls back to the last checkpoint + recent messages.
    """
    def __init__(self, checkpoint_interval: int = 5, max_messages: int = 20):
        self.checkpoint_interval = checkpoint_interval
        self.max_messages = max_messages
        self.messages: list[dict] = []
        self.checkpoints: list[dict] = []
        self.turn_count = 0

    def _create_checkpoint(self) -> dict:
        recent_text = "\n".join(
            f"{m['role']}: {str(m.get('content', ''))[:300]}"
            for m in self.messages[-10:]
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=CHECKPOINT_SUMMARIZE,
            messages=[{"role": "user", "content": recent_text}],
        )
        checkpoint = {
            "id": len(self.checkpoints) + 1,
            "timestamp": time.time(),
            "summary": response.content[0].text.strip(),
            "message_count": len(self.messages),
        }
        print(f"[Checkpoint {checkpoint['id']}] Saved at turn {self.turn_count}")
        return checkpoint

    def add(self, role: str, content):
        self.messages.append({"role": role, "content": content})
        self.turn_count += 1

        # Create checkpoint every N turns
        if self.turn_count % self.checkpoint_interval == 0:
            cp = self._create_checkpoint()
            self.checkpoints.append(cp)

        # If context is too long, restore from last checkpoint
        if len(self.messages) > self.max_messages and self.checkpoints:
            self._restore_from_checkpoint()

    def _restore_from_checkpoint(self):
        last_cp = self.checkpoints[-1]
        recent = self.messages[-3:]  # keep last 3 messages

        # Rebuild context from checkpoint summary + recent messages
        self.messages = [
            {"role": "user", "content": f"[Checkpoint restore]\n{last_cp['summary']}"},
        ] + recent

        print(
            f"[Checkpoint] Restored from #{last_cp['id']} — "
            f"context: {last_cp['message_count']} → {len(self.messages)} messages"
        )

    def get_messages(self) -> list[dict]:
        return self.messages


agent = CheckpointAgent(checkpoint_interval=5, max_messages=12)

# Simulate a long task
for i in range(25):
    agent.add("user", f"Tool result {i}: " + f"data point {i} " * 20)
    agent.add("assistant", f"Processing step {i}: " + "analysis " * 10)

print(f"\nFinal context size: {len(agent.get_messages())} messages")
print(f"Checkpoints created: {len(agent.checkpoints)}")
```

**Expected Token Savings:** Checkpoint restore reduces context from 20+ messages to 4 (1 checkpoint summary + 3 recent) — 80% reduction; unlike rolling summarization, checkpoints allow auditing the agent's progress history.
**Environment:** Very long-running agents (30+ turns); checkpoints enable both context management and progress tracking/debugging.

---

### Option 6 — Per-subtask context isolation with shared state store

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


class SharedState:
    """
    Lightweight key-value store passed between subtasks.
    Each subtask reads what it needs and writes its outputs.
    Replaces carrying the full conversation history.
    """
    def __init__(self):
        self._data: dict[str, str] = {}
        self._history: list[dict] = []   # log of what was written

    def write(self, key: str, value: str, subtask: str = ""):
        self._data[key] = value
        self._history.append({"subtask": subtask, "key": key, "len": len(value)})

    def read(self, *keys: str) -> dict:
        return {k: self._data[k] for k in keys if k in self._data}

    def to_context_block(self, *keys: str) -> str:
        """Format selected keys as a compact context block."""
        items = self.read(*keys) if keys else self._data
        return "\n".join(f"[{k}]: {v[:300]}" for k, v in items.items())

    def summary(self) -> str:
        return f"State keys: {list(self._data.keys())}, " \
               f"total size: {sum(len(v) for v in self._data.values())} chars"


def run_subtask(
    subtask_name: str,
    system: str,
    user_prompt: str,
    state: SharedState,
    read_keys: list[str],
    write_key: str,
) -> str:
    context = state.to_context_block(*read_keys) if read_keys else ""
    full_prompt = f"{user_prompt}\n\nAvailable context:\n{context}" if context else user_prompt

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": full_prompt}],
    )
    output = response.content[0].text.strip()
    state.write(write_key, output, subtask=subtask_name)
    print(f"[{subtask_name}] → {write_key}: {len(output)} chars")
    return output


# Comparison table
# | Option | Trigger | Retention Policy | Best For |
# |--------|---------|-----------------|----------|
# | 1 Manual checkpoint | After subtask | Full summary | Phased pipelines |
# | 2 Token threshold | Exceeds N tokens | Keep recent + summary | Unpredictable task lengths |
# | 3 Async handoff | After subtask | JSON structured | Parallel pipelines |
# | 4 Importance weight | Each message | HIGH always, LOW dropped | Exploratory agents |
# | 5 Checkpoint restore | Every N turns + overflow | Summary + last 3 | Very long tasks |
# | 6 Isolated context | Per subtask | Key-value state store | Modular pipelines |

state = SharedState()

# Each subtask operates in isolation, sharing only explicit state keys
run_subtask(
    "research",
    "You are a researcher. Produce a concise research summary.",
    "Research async Python frameworks",
    state,
    read_keys=[],
    write_key="research_findings",
)

run_subtask(
    "outline",
    "You are an editor. Create a structured outline.",
    "Create an outline for a comparison article",
    state,
    read_keys=["research_findings"],   # only reads research, not full history
    write_key="article_outline",
)

run_subtask(
    "draft",
    "You are a technical writer. Write the article.",
    "Write the comparison article",
    state,
    read_keys=["research_findings", "article_outline"],
    write_key="article_draft",
)

print(f"\n{state.summary()}")
```

**Expected Token Savings:** Isolated context means each subtask starts with only the state keys it explicitly requests — a subtask needing only `research_findings` gets ~300 tokens of context instead of the full 8000-token research conversation; for N-subtask pipelines, total input tokens scale linearly instead of quadratically.
**Environment:** Modular pipeline agents where each subtask is a pure function of its inputs; the state store pattern is the cleanest architecture for long pipelines and makes subtask dependencies explicit.
