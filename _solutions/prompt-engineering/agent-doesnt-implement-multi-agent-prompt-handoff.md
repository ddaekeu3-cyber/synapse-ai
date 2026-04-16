---
layout: solution
title: "Agent Doesn't Implement Multi-Agent Prompt Handoff"
category: prompt-engineering
description: "Structure prompts so one agent's output becomes a well-formed input for the next — with context summaries, role transitions, state serialization, and handoff contracts that prevent information loss between agent boundaries."
tags: [prompt-engineering, multi-agent, handoff, orchestration, context, python]
---

# Agent Doesn't Implement Multi-Agent Prompt Handoff

Agents that pass raw output between each other lose structured context, duplicate information in every call, and drift from the original task intent. A handoff protocol serializes exactly what the receiving agent needs — in the format it expects — while stripping what it doesn't, so each agent starts with a clean, complete context.

## Option 1: Structured Handoff via XML Tags

```python
import anthropic
import re

client = anthropic.Anthropic()

def agent_a_plan(task: str) -> dict:
    """Agent A: planning agent. Outputs structured XML handoff."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a planning agent. Break tasks into steps. Always output a handoff block.",
        messages=[{
            "role": "user",
            "content": (
                f"Task: {task}\n\n"
                "Plan this task and output:\n"
                "<handoff>\n"
                "  <task_summary>one sentence</task_summary>\n"
                "  <steps>numbered list of steps</steps>\n"
                "  <constraints>any constraints</constraints>\n"
                "  <next_agent>executor</next_agent>\n"
                "</handoff>"
            ),
        }],
    )
    raw = resp.content[0].text
    # Extract handoff block
    match = re.search(r"<handoff>(.*?)</handoff>", raw, re.DOTALL)
    handoff_xml = match.group(0) if match else f"<handoff><task_summary>{task}</task_summary></handoff>"
    return {"raw": raw, "handoff": handoff_xml, "tokens": resp.usage.output_tokens}

def agent_b_execute(handoff_xml: str) -> dict:
    """Agent B: execution agent. Receives structured handoff from Agent A."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are an executor agent. You receive a structured plan and implement it.",
        messages=[{
            "role": "user",
            "content": (
                "You are receiving a handoff from the planning agent:\n\n"
                f"{handoff_xml}\n\n"
                "Execute the plan. For each step, confirm completion or flag blockers."
            ),
        }],
    )
    return {"result": resp.content[0].text, "tokens": resp.usage.output_tokens}

task = "Set up a Python project with tests and CI configuration"
print(f"Task: {task}\n")

plan_result = agent_a_plan(task)
print(f"Agent A (planner) output ({plan_result['tokens']} tokens):")
print(f"  Handoff: {plan_result['handoff'][:120]!r}\n")

exec_result = agent_b_execute(plan_result["handoff"])
print(f"Agent B (executor) output ({exec_result['tokens']} tokens):")
print(f"  {exec_result['result'][:150]}")

# Expected Token Savings: Handoff XML strips Agent A's reasoning; Agent B receives only structured plan (~40% fewer tokens)
# Environment: XML tags are reliable for Claude; use JSON for machine-parsed handoffs
```

## Option 2: JSON Handoff Contract with Schema Validation

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

HANDOFF_SCHEMA = {
    "task_id":     "string — unique ID for this task chain",
    "intent":      "string — original user intent in one sentence",
    "completed":   "list[str] — steps already completed",
    "pending":     "list[str] — steps remaining",
    "artifacts":   "dict — key outputs produced so far",
    "next_role":   "string — role of next agent (reviewer|executor|summarizer)",
    "context_key": "string — most important fact for next agent",
}

def call_with_handoff_output(role: str, system: str, user_msg: str) -> dict:
    """Call model, extract JSON handoff from response."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            handoff = json.loads(match.group())
            return {"text": text, "handoff": handoff, "valid": True}
        except json.JSONDecodeError:
            pass
    return {"text": text, "handoff": {}, "valid": False}

def validate_handoff(handoff: dict) -> list[str]:
    required = ["task_id", "intent", "completed", "pending", "next_role"]
    return [k for k in required if k not in handoff]

import uuid

task_id = str(uuid.uuid4())[:8]
intent  = "Build a REST API for a todo app"

# Agent 1: architect
arch_result = call_with_handoff_output(
    role="architect",
    system="You are a software architect. Design systems and hand off to implementers.",
    user_msg=(
        f"Design a REST API for: {intent}\n\n"
        f"Output a JSON handoff (schema: {json.dumps(HANDOFF_SCHEMA)}):\n"
        f"Use task_id={task_id!r}, next_role='executor'."
    ),
)
handoff = arch_result["handoff"]
missing = validate_handoff(handoff)
print(f"Architect handoff valid={arch_result['valid']} missing={missing}")
if handoff:
    print(f"  pending steps: {handoff.get('pending', [])[:2]}")
    print(f"  context_key:   {handoff.get('context_key', '')[:60]!r}")

# Agent 2: executor receives validated handoff
if not missing:
    exec_result = call_with_handoff_output(
        role="executor",
        system="You are an implementer. You receive a design handoff and implement it.",
        user_msg=(
            f"Handoff received:\n{json.dumps(handoff, indent=2)}\n\n"
            "Implement the first pending step. Output a JSON handoff with your progress."
        ),
    )
    print(f"\nExecutor handoff valid={exec_result['valid']}")
    print(f"  completed: {exec_result['handoff'].get('completed', [])[:2]}")

# Expected Token Savings: JSON handoff is 60-80% smaller than passing full conversation history
# Environment: validate_handoff() runs in <1ms; add jsonschema for strict type validation
```

## Option 3: Role-Aware Context Compression Before Handoff

```python
import anthropic

client = anthropic.Anthropic()

def compress_for_handoff(full_context: str, receiving_role: str, budget_chars: int = 1000) -> str:
    """Use a cheap model to compress context for the next agent's specific needs."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f"You are compressing a context for handoff to a {receiving_role} agent.\n"
                f"Keep ONLY what a {receiving_role} needs. Max {budget_chars} characters.\n"
                f"Omit: reasoning, alternatives considered, meta-commentary.\n"
                f"Keep: decisions made, current state, constraints, next action.\n\n"
                f"Full context:\n{full_context}\n\n"
                "Output compressed handoff context:"
            ),
        }],
    )
    return resp.content[0].text.strip()

# Simulate a long planning session
planning_session = """
We started by analyzing the requirements for a data pipeline.
After extensive discussion, we considered three approaches:
1. Batch processing with Spark
2. Stream processing with Kafka
3. Simple queue-based approach

We spent time evaluating costs, team expertise, and latency requirements.
The team has no Kafka experience, so option 3 was ruled out first.
Spark is powerful but overkill for our 10k events/day volume.
We eventually decided on a simple SQLite queue with a Python worker.

Decision: SQLite queue + Python worker process.
Constraints: Must complete in 2 weeks, budget $0 infrastructure cost.
Next step: Implement the queue schema and first worker.
Current state: No code written yet, requirements finalized.
"""

# Compress for two different receiving roles
executor_context  = compress_for_handoff(planning_session, "executor",  budget_chars=400)
reviewer_context  = compress_for_handoff(planning_session, "reviewer",  budget_chars=400)

print(f"Original: {len(planning_session)} chars")
print(f"\nFor executor ({len(executor_context)} chars):")
print(f"  {executor_context[:200]}")
print(f"\nFor reviewer ({len(reviewer_context)} chars):")
print(f"  {reviewer_context[:200]}")

# Verify executor gets useful context
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=150,
    messages=[{
        "role": "user",
        "content": f"Context:\n{executor_context}\n\nWhat should I implement first?",
    }],
)
print(f"\nExecutor first action: {resp.content[0].text[:100]}")

# Expected Token Savings: 400-char handoff vs 1000-char original = 60% input token reduction per downstream call
# Environment: compression adds one Haiku call (~$0.00004); saves multiple downstream calls that each see less context
```

## Option 4: Stateful Handoff Chain with SQLite Checkpoint

```python
import anthropic
import sqlite3
import json
import time
import uuid

client = anthropic.Anthropic()
DB = "handoff_chain.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS handoffs (
            chain_id TEXT, step INTEGER, from_role TEXT, to_role TEXT,
            payload TEXT, ts REAL, tokens_in INTEGER, tokens_out INTEGER
        )
    """)
    con.commit(); con.close()

def record_handoff(chain_id: str, step: int, from_role: str, to_role: str,
                   payload: dict, tokens_in: int, tokens_out: int):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?)",
        (chain_id, step, from_role, to_role, json.dumps(payload), time.time(), tokens_in, tokens_out),
    )
    con.commit(); con.close()

def load_chain(chain_id: str) -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT step, from_role, to_role, payload FROM handoffs WHERE chain_id=? ORDER BY step",
        (chain_id,),
    ).fetchall()
    con.close()
    return [{"step": r[0], "from": r[1], "to": r[2], "payload": json.loads(r[3])} for r in rows]

def agent_step(chain_id: str, step: int, role: str, next_role: str,
               system: str, user_msg: str) -> dict:
    """Run one agent step, checkpoint handoff to SQLite."""
    init_db()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text
    payload = {
        "output": text[:500],
        "role": role,
        "next_role": next_role,
        "step": step,
    }
    record_handoff(chain_id, step, role, next_role, payload,
                   resp.usage.input_tokens, resp.usage.output_tokens)
    return payload

chain_id = str(uuid.uuid4())[:8]
task     = "Create a Python function that validates email addresses"

# Step 1: Analyst
step1 = agent_step(
    chain_id, step=1, role="analyst", next_role="coder",
    system="You are a requirements analyst. Define what needs to be built.",
    user_msg=f"Task: {task}\nList the validation requirements (max 5 bullet points).",
)
print(f"Step 1 (analyst): {step1['output'][:120]!r}")

# Step 2: Coder (receives analyst output)
step2 = agent_step(
    chain_id, step=2, role="coder", next_role="reviewer",
    system="You are a Python developer. Implement based on requirements.",
    user_msg=(
        f"Requirements from analyst:\n{step1['output']}\n\n"
        "Write the Python function (no tests, just the function)."
    ),
)
print(f"\nStep 2 (coder): {step2['output'][:120]!r}")

# Step 3: Reviewer (receives coder output)
step3 = agent_step(
    chain_id, step=3, role="reviewer", next_role="done",
    system="You are a code reviewer. Give exactly 2 improvement suggestions.",
    user_msg=f"Review this code:\n{step2['output']}\n\nTwo improvements:",
)
print(f"\nStep 3 (reviewer): {step3['output'][:120]!r}")

# Show chain history
chain = load_chain(chain_id)
print(f"\nChain {chain_id}: {len(chain)} steps")
for h in chain:
    print(f"  Step {h['step']}: {h['from']} -> {h['to']}")

# Expected Token Savings: Each step receives only prior step output, not full chain history — O(1) vs O(N) context growth
# Environment: SQLite checkpoint enables chain resumption after crashes; replay from any step
```

## Option 5: Handoff with Persona Transition

```python
import anthropic

client = anthropic.Anthropic()

PERSONA_PROMPTS = {
    "researcher": (
        "You are a researcher. You gather facts and summarize findings. "
        "When handing off, structure your output as: FINDINGS: [...] OPEN_QUESTIONS: [...]"
    ),
    "writer": (
        "You are a technical writer. You receive research findings and write clear documentation. "
        "When handing off, include: DRAFT: [...] NEEDS_REVIEW: [...]"
    ),
    "editor": (
        "You are an editor. You receive drafts and improve clarity, fix errors, and finalize. "
        "Output the final polished version."
    ),
}

def persona_call(role: str, incoming_context: str, task: str) -> dict:
    system = PERSONA_PROMPTS[role]
    # Build a clean handoff prompt that establishes the context boundary
    handoff_header = (
        f"--- HANDOFF TO {role.upper()} ---\n"
        f"Incoming context from previous agent:\n{incoming_context}\n"
        f"--- YOUR TASK ---\n{task}\n"
    ) if incoming_context else task

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        system=system,
        messages=[{"role": "user", "content": handoff_header}],
    )
    return {
        "role": role,
        "output": resp.content[0].text,
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
    }

topic = "Python asyncio event loop internals"

# Pipeline: researcher -> writer -> editor
r1 = persona_call("researcher", "", f"Research key facts about: {topic}")
print(f"[researcher] {r1['tokens_in']}in/{r1['tokens_out']}out")
print(f"  {r1['output'][:120]!r}\n")

r2 = persona_call("writer", r1["output"],
                  "Write a 2-paragraph technical explanation for developers.")
print(f"[writer] {r2['tokens_in']}in/{r2['tokens_out']}out")
print(f"  {r2['output'][:120]!r}\n")

r3 = persona_call("editor", r2["output"], "Polish and finalize the draft.")
print(f"[editor] {r3['tokens_in']}in/{r3['tokens_out']}out")
print(f"  {r3['output'][:120]!r}")

total_in  = r1["tokens_in"]  + r2["tokens_in"]  + r3["tokens_in"]
total_out = r1["tokens_out"] + r2["tokens_out"] + r3["tokens_out"]
print(f"\nTotal: {total_in}in / {total_out}out tokens across 3 agents")

# Expected Token Savings: Each agent receives only prior agent's output, not all prior outputs — linear not quadratic growth
# Environment: PERSONA_PROMPTS stored in config; swap model per role (haiku for researcher, sonnet for editor)
```

## Option 6: Handoff Quality Evaluator — Score Before Passing

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def evaluate_handoff_quality(handoff_text: str, expected_fields: list[str]) -> dict:
    """Use a cheap model to score handoff quality before passing to next agent."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Evaluate this agent handoff for quality. Expected fields: {expected_fields}\n\n"
                f"Handoff:\n{handoff_text}\n\n"
                "Score 0-10 and respond as JSON:\n"
                '{"score": 0-10, "present": ["field1"], "missing": ["field2"], "issues": ["..."]}',
            ),
        }],
    )
    text = resp.content[0].text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"score": 0, "present": [], "missing": expected_fields, "issues": ["parse error"]}

def agent_with_quality_gate(
    system: str, user_msg: str,
    expected_fields: list[str],
    min_score: int = 7,
    max_retries: int = 2,
) -> tuple[str, dict]:
    """Generate handoff, evaluate quality, retry if below threshold."""
    for attempt in range(max_retries + 1):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        output = resp.content[0].text
        quality = evaluate_handoff_quality(output, expected_fields)
        score = quality.get("score", 0)

        print(f"  Attempt {attempt+1}: score={score}/10 "
              f"missing={quality.get('missing', [])} "
              f"issues={quality.get('issues', [])[:1]}")

        if score >= min_score:
            return output, quality

        # Retry with quality feedback
        user_msg = (
            f"{user_msg}\n\n"
            f"Previous attempt score: {score}/10. "
            f"Missing: {quality.get('missing', [])}. "
            f"Issues: {quality.get('issues', [])}. "
            "Please improve the handoff to include all required fields."
        )

    return output, quality  # return best effort after max retries

# Test: planning agent with quality gate
system = "You are a planning agent. Output a structured handoff with all required fields."
user_msg = (
    "Plan building a user authentication system.\n"
    "Required handoff fields: task_summary, steps, constraints, next_role, estimated_effort."
)
required = ["task_summary", "steps", "constraints", "next_role", "estimated_effort"]

print("Running agent with quality gate:")
output, quality = agent_with_quality_gate(system, user_msg, required, min_score=6)
print(f"\nFinal score: {quality.get('score')}/10")
print(f"Output preview: {output[:150]!r}")

# Expected Token Savings: Quality gate catches low-quality handoffs before they corrupt downstream agents
# Environment: evaluate_handoff_quality costs ~1 Haiku call; worth it to prevent downstream retry loops
```

## Comparison

| Option | Handoff Format | Context Compression | Quality Gate | State Persistence |
|--------|---------------|---------------------|-------------|------------------|
| 1 — XML Tags | Structured XML | No | No | No |
| 2 — JSON Contract | Validated JSON | No | Schema check | No |
| 3 — Role-Aware Compression | Free text | LLM-compressed | No | No |
| 4 — SQLite Checkpoint | JSON payload | Step-by-step | No | SQLite |
| 5 — Persona Transition | Free text | Step output only | No | No |
| 6 — Quality Evaluator | Free text | No | Score + retry | No |
