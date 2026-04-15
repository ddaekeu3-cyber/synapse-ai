---
layout: solution
title: "Agent Oscillates Between Two Conflicting Decisions"
category: loop-stuck
description: "Agent picks option A, then reconsiders and picks B, then switches back to A — looping indefinitely without committing to either."
tags: [loop-stuck, decision-making, reliability, agentic, prompt-engineering]
---

## Symptom

In a multi-step agentic task, the agent commits to a plan, calls a tool, then immediately reconsiders when the tool returns. It switches strategy, calls a different tool, reconsiders again, and switches back. This back-and-forth continues until the step limit is hit or the user intervenes. The task never completes; only tokens are consumed.

## Root Cause

The model lacks a commitment mechanism. Each new tool result is a fresh prompt context that may trigger re-evaluation of earlier decisions. Without an explicit "stick with the plan" instruction, the model optimises locally at each step — potentially undoing decisions made at previous steps. Uncertainty, ambiguous tool results, or competing valid strategies all amplify this effect.

## Fix

### Option 1 — Commit-and-proceed instruction in the system prompt

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a task-execution agent.

Decision policy:
- At the start of each task, form a plan and commit to it.
- Once you have called a tool and received a result, proceed to the NEXT step.
- Do NOT re-evaluate your earlier decisions unless a tool returns a clear error.
- "Reconsidering" and "let me think again" are not valid steps — act or stop.
- If you are uncertain between two approaches, pick one and proceed.
- Record your plan in your first response and follow it exactly."""

def run_agent(task: str, tools: list[dict], tool_handler, max_steps: int = 10) -> str:
    messages = [{"role": "user", "content": task}]
    for step in range(max_steps):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = tool_handler(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                print(f"[step {step}] {block.name}({block.input}) → {str(result)[:60]}")
        if results:
            messages.append({"role": "user", "content": results})
    return "max steps reached"

import json
TOOLS = [
    {
        "name": "search_records",
        "description": "Search records by field.",
        "input_schema": {"type": "object", "properties": {"field": {"type": "string"}, "value": {"type": "string"}}, "required": ["field", "value"]},
    },
    {
        "name": "update_record",
        "description": "Update a record.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}, "data": {"type": "object"}}, "required": ["id", "data"]},
    },
]

def handler(name, inputs):
    if name == "search_records":  return json.dumps([{"id": "r1", "status": "pending"}])
    if name == "update_record":   return json.dumps({"ok": True, "id": inputs["id"]})
    return json.dumps({"error": "unknown"})

print(run_agent("Find pending records and mark them as processed.", TOOLS, handler))
```

**Expected Token Savings:** Commit-and-proceed eliminates re-evaluation turns; each decision is made once, not repeatedly.
**Environment:** All multi-step agentic tasks; commit policy belongs in every agent's system prompt.

---

### Option 2 — Explicit plan step tracking: cross off completed steps

```python
import json
import anthropic

client = anthropic.Anthropic()

PLAN_SYSTEM = """You are a plan-following agent.

At the start of each task:
1. Output a numbered plan (e.g., "1. Search users. 2. Filter. 3. Update.")
2. Execute step 1. When done, say "✓ Step 1 complete."
3. Execute step 2. When done, say "✓ Step 2 complete."
4. Continue until all steps are marked complete.

Rules:
- Never re-do a completed step.
- Never switch strategies mid-plan.
- If a step fails, say "✗ Step N failed: [reason]" and stop."""

import json

TOOLS = [
    {
        "name": "list_users",
        "description": "List all users.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_notification",
        "description": "Send a notification to a user.",
        "input_schema": {
            "type": "object",
            "required": ["user_id", "message"],
            "properties": {"user_id": {"type": "string"}, "message": {"type": "string"}},
        },
    },
]

def handler(name, inputs):
    if name == "list_users":
        return json.dumps([{"id": "u1", "name": "Alice"}, {"id": "u2", "name": "Bob"}])
    if name == "send_notification":
        return json.dumps({"ok": True, "sent_to": inputs["user_id"]})
    return json.dumps({"error": "unknown"})

def run_plan_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    for step in range(12):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=PLAN_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if b.type == "text"), "")
            print(f"[step {step}] {text[:80]}")
            if "✓" in text or "complete" in text.lower():
                continue   # agent is narrating completion
            return text

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handler(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        if results:
            messages.append({"role": "user", "content": results})
    return "max steps"

print(run_plan_agent("List all users and send each a welcome notification."))
```

**Expected Token Savings:** Step-tracking forces linear progression; completed steps are not revisited.
**Environment:** Tasks with 3+ steps where re-evaluation is common; plan-and-execute is the canonical pattern.

---

### Option 3 — Decision log: record and enforce prior decisions

```python
import json
import anthropic

client = anthropic.Anthropic()

class DecisionLog:
    def __init__(self):
        self._decisions: list[dict] = []

    def record(self, topic: str, choice: str, rationale: str = "") -> None:
        self._decisions.append({"topic": topic, "choice": choice, "rationale": rationale})
        print(f"[decision] {topic!r} → {choice!r}")

    def as_context(self) -> str:
        if not self._decisions:
            return "No decisions made yet."
        return "COMMITTED DECISIONS (do not reverse):\n" + "\n".join(
            f"- {d['topic']}: {d['choice']}" + (f" ({d['rationale']})" if d['rationale'] else "")
            for d in self._decisions
        )

log = DecisionLog()

SYSTEM_TEMPLATE = """You are a decision-committed agent.

{decisions}

Rules:
- If a prior decision covers the current step, follow it without reconsidering.
- Only make new decisions; never undo committed ones.
- If you make a new decision, state it clearly: "DECISION: [topic] → [choice]"."""

TOOLS = [
    {
        "name": "query_data",
        "description": "Query data with a strategy.",
        "input_schema": {
            "type": "object",
            "properties": {"strategy": {"type": "string", "enum": ["fast", "thorough"]}},
            "required": ["strategy"],
        },
    },
    {
        "name": "write_output",
        "description": "Write the final output.",
        "input_schema": {
            "type": "object",
            "properties": {"format": {"type": "string", "enum": ["json", "csv", "text"]}, "data": {"type": "string"}},
            "required": ["format", "data"],
        },
    },
]

def handler(name, inputs):
    if name == "query_data":
        return json.dumps({"records": 42, "strategy_used": inputs["strategy"]})
    if name == "write_output":
        return json.dumps({"ok": True, "format": inputs["format"], "chars": len(inputs["data"])})
    return json.dumps({"error": "unknown"})

def parse_decisions(text: str) -> list[tuple[str, str]]:
    """Extract DECISION: statements from agent output."""
    import re
    return re.findall(r"DECISION:\s*([^→]+)\s*→\s*(.+)", text)

def run_committed_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    for step in range(10):
        system = SYSTEM_TEMPLATE.format(decisions=log.as_context())
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        # Parse and record decisions from the response text
        for block in response.content:
            if block.type == "text":
                for topic, choice in parse_decisions(block.text):
                    log.record(topic.strip(), choice.strip())

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": handler(b.name, b.input)}
            for b in response.content if b.type == "tool_use"
        ]
        if results:
            messages.append({"role": "user", "content": results})
    return "max steps"

print(run_committed_agent("Analyse the data using an appropriate strategy and produce a report."))
```

**Expected Token Savings:** Decision log prevents re-evaluation of settled choices; each avoided oscillation saves 1–3 turns.
**Environment:** Complex agents with multiple branching decision points; decision log is especially useful for long-running tasks.

---

### Option 4 — Oscillation detector: inject a tiebreaker when loop is detected

```python
import json
import hashlib
import anthropic

client = anthropic.Anthropic()

def fingerprint(content) -> str:
    return hashlib.md5(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:8]

class OscillationDetector:
    def __init__(self, window: int = 4):
        self._prints:  list[str] = []
        self._window   = window

    def record(self, tool_name: str, inputs: dict) -> bool:
        fp = f"{tool_name}:{fingerprint(inputs)}"
        self._prints.append(fp)
        if len(self._prints) < self._window:
            return False
        recent = self._prints[-self._window:]
        # Detect ABAB pattern (oscillation between 2 states)
        if len(set(recent)) <= 2 and recent[0] == recent[2] and recent[1] == recent[3]:
            print(f"[oscillation] detected: {recent}")
            return True
        return False

TOOLS = [
    {
        "name": "try_approach_a",
        "description": "Try approach A.",
        "input_schema": {"type": "object", "properties": {"param": {"type": "string"}}, "required": ["param"]},
    },
    {
        "name": "try_approach_b",
        "description": "Try approach B.",
        "input_schema": {"type": "object", "properties": {"param": {"type": "string"}}, "required": ["param"]},
    },
]

def handler(name, inputs):
    # Both approaches return ambiguous results to trigger oscillation
    return json.dumps({"result": "ambiguous", "approach": name, "confidence": 0.5})

TIEBREAKER = "You have alternated between the same two approaches. COMMIT to approach A and proceed without reconsidering."

def run_with_oscillation_detection(task: str) -> str:
    messages  = [{"role": "user", "content": task}]
    detector  = OscillationDetector(window=4)
    tiebroken = False

    for step in range(12):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if not tiebroken and detector.record(block.name, block.input):
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": TIEBREAKER})
                    tiebroken = True
                else:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(handler(block.name, block.input))})
        messages.append({"role": "user", "content": results})
    return "max steps"

print(run_with_oscillation_detection("Find the best approach to process this data."))
```

**Expected Token Savings:** Oscillation detection injects a tiebreaker at the moment of detection; stops the loop within 2 additional steps.
**Environment:** Automated pipelines with no human monitoring; detector runs silently and intervenes only when needed.

---

### Option 5 — First-answer commitment: lock the first tool choice

```python
import json
import anthropic

client = anthropic.Anthropic()

class FirstChoiceLock:
    """After the first tool call, lock to that tool family."""

    def __init__(self, lock_after_first: bool = True):
        self._locked_tool: str | None = None
        self._lock = lock_after_first

    def record(self, tool_name: str) -> None:
        if self._lock and self._locked_tool is None:
            self._locked_tool = tool_name
            print(f"[lock] committed to tool: {tool_name!r}")

    def is_allowed(self, tool_name: str) -> bool:
        if not self._lock or self._locked_tool is None:
            return True
        return tool_name == self._locked_tool

TOOLS = [
    {
        "name": "search_v1",
        "description": "Search using algorithm V1 (fast, less precise).",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "search_v2",
        "description": "Search using algorithm V2 (slower, more precise).",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
]

def handler(name, inputs):
    return json.dumps({"results": [f"result-{i}" for i in range(3)], "algo": name})

DENIED = json.dumps({"error": "You have already committed to a different search algorithm. Continue with your first choice."})

def run_with_lock(task: str) -> str:
    lock     = FirstChoiceLock()
    messages = [{"role": "user", "content": task}]
    for _ in range(10):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                lock.record(block.name)
                if lock.is_allowed(block.name):
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(handler(block.name, block.input))})
                else:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": DENIED})
        messages.append({"role": "user", "content": results})
    return "max steps"

print(run_with_lock("Search for 'machine learning' using the best available algorithm."))
```

**Expected Token Savings:** First-choice lock prevents switching costs; if the agent switches, it gets a denied message that costs ~20 tokens instead of a full tool call.
**Environment:** Tasks with alternative tool strategies where early commitment prevents thrashing.

---

### Option 6 — Temperature zero + structured choice forcing

```python
import json
import anthropic

client = anthropic.Anthropic()

CHOICE_SYSTEM = """You are a decisive planning agent. When facing a choice between approaches:

1. List the options briefly (1 sentence each).
2. Score each on: speed (1-5), reliability (1-5), simplicity (1-5).
3. Pick the HIGHEST total score. If tied, pick the first.
4. State: "COMMITTED CHOICE: [option name]"
5. Proceed immediately with the committed choice.

Do not revisit this decision."""

def force_commitment(options: list[str], context: str) -> str:
    """Force a committed choice from a list of options."""
    prompt = (
        f"Context: {context}\n\n"
        f"Options:\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CHOICE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text
    import re
    match = re.search(r"COMMITTED CHOICE:\s*(.+)", text)
    committed = match.group(1).strip() if match else options[0]
    print(f"[decision] committed to: {committed!r}")
    return committed

def run_decided_agent(task: str) -> str:
    # Pre-decide strategy before entering the agentic loop
    strategy = force_commitment(
        ["Use full-text search (fast, approximate)",
         "Use semantic similarity search (slower, precise)"],
        context=task,
    )

    system = f"""You are a task agent. You have committed to this strategy: {strategy!r}
Execute it without reconsidering. Do not switch strategies."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

print(run_decided_agent("Find documents related to 'climate change policy' in our knowledge base."))
```

**Expected Token Savings:** Pre-committing before the agentic loop starts prevents oscillation entirely; the loop never sees the choice.
**Environment:** Tasks where the strategy choice is separable from the execution; front-load the decision.

---

## Comparison

| Option | Intervention Point | Human Required? | Works Autonomously? | Best For |
|---|---|---|---|---|
| 1. Commit-and-proceed prompt | System prompt | No | Yes | Universal baseline |
| 2. Step tracking | Narration protocol | No | Yes | Multi-step sequential tasks |
| 3. Decision log | Per-decision recording | No | Yes | Complex branching decisions |
| 4. Oscillation detector | Loop detection | No | Yes | Automated pipelines |
| 5. First-choice lock | Tool-layer enforcement | No | Yes | Alternative-tool thrashing |
| 6. Pre-commitment forcing | Before loop starts | No | Yes | Strategy-level decisions |
