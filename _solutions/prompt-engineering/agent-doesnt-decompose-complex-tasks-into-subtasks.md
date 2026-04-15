---
layout: solution
title: "Agent Doesn't Decompose Complex Tasks Into Subtasks"
category: prompt-engineering
description: "Agent attempts to solve multi-step problems in a single pass, producing incomplete or incoherent output instead of breaking the work into manageable, verifiable steps."
tags: [prompt-engineering, reasoning, decomposition, reliability, production]
---

## Symptom

The user asks the agent to "analyse our Q3 sales data, identify the top 3 failure modes, write a root-cause analysis for each, and draft remediation tickets." The agent produces a single rambling response that superficially addresses each part but skips critical steps, conflates findings across sections, and leaves the user with output they can't act on. The agent attempted the whole task in one cognitive step rather than treating it as a pipeline.

## Root Cause

LLMs are stateless next-token predictors. Without an explicit decomposition step, the model allocates attention across the entire complex prompt and produces a blend of partial answers. Planning, execution, and verification all compete for the same context window in a single forward pass. Breaking the task into explicit subtasks forces the model to commit to each step's output before moving to the next — the same way a human expert would outline a document before writing it.

## Fix

### Option 1 — Ask the agent to plan before acting

```python
import anthropic
import json

client = anthropic.Anthropic()

def plan_then_execute(complex_task: str) -> dict:
    """
    Two-pass approach:
    Pass 1: Generate a numbered subtask list
    Pass 2: Execute each subtask in order, feeding results forward
    """
    # Pass 1: decompose
    plan_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Break down this task into 3-6 numbered subtasks. "
                f"Each subtask must be independently completable and produce a concrete output.\n\n"
                f"Task: {complex_task}\n\n"
                f"Return a JSON array of subtask strings only."
            ),
        }],
    )
    try:
        subtasks: list[str] = json.loads(plan_response.content[0].text)
    except json.JSONDecodeError:
        # Fallback: parse numbered lines
        lines = plan_response.content[0].text.strip().splitlines()
        subtasks = [l.lstrip("0123456789. ") for l in lines if l.strip()]

    print(f"[plan] decomposed into {len(subtasks)} subtasks:")
    for i, s in enumerate(subtasks, 1):
        print(f"  {i}. {s}")

    # Pass 2: execute each subtask, accumulating context
    results = []
    accumulated_context = ""

    for i, subtask in enumerate(subtasks, 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Overall goal: {complex_task}\n\n"
                    f"{'Previous results:\n' + accumulated_context + chr(10) if accumulated_context else ''}"
                    f"Current subtask ({i}/{len(subtasks)}): {subtask}\n\n"
                    f"Complete only this subtask. Be concise and specific."
                ),
            }],
        )
        result = response.content[0].text.strip()
        results.append({"subtask": subtask, "result": result})
        accumulated_context += f"\nSubtask {i} result: {result[:200]}"
        print(f"[execute] subtask {i}/{len(subtasks)}: {result[:80]}...")

    return {"subtasks": subtasks, "results": results}

output = plan_then_execute(
    "Analyse the pros and cons of microservices vs monolith architecture, "
    "recommend one for a 5-person startup, and draft a one-page migration checklist."
)
print(f"\n[done] {len(output['results'])} subtasks completed")
```

**Expected Token Savings:** Planning prevents the model from wasting tokens on a confused single-pass attempt; subtask outputs are shorter and more accurate than one sprawling response — reducing total tokens while improving quality.
**Environment:** Complex analytical tasks, multi-section document generation, research pipelines.

---

### Option 2 — ReAct-style think-act-observe loop

```python
import anthropic
import json

client = anthropic.Anthropic()

def react_loop(task: str, max_steps: int = 8) -> str:
    """
    ReAct pattern: Reason → Act → Observe → Reason → Act → ...
    Each step is explicit: the model states what it's thinking,
    what action it takes, and what it observes.
    """
    messages = [{
        "role": "user",
        "content": (
            f"Solve this step by step using the following format for each step:\n"
            f"Thought: [what you're reasoning about]\n"
            f"Action: [what you're doing — e.g. 'analyse', 'calculate', 'draft', 'verify']\n"
            f"Observation: [what you found or produced]\n\n"
            f"When you have a final answer, write: Final Answer: [answer]\n\n"
            f"Task: {task}"
        ),
    }]

    full_reasoning = []
    for step in range(max_steps):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=messages,
        )
        text = response.content[0].text.strip()
        full_reasoning.append(text)
        print(f"\n--- Step {step + 1} ---\n{text[:300]}")

        if "Final Answer:" in text:
            final = text.split("Final Answer:")[-1].strip()
            print(f"\n[react] completed in {step + 1} steps")
            return final

        # Feed the observation back as context for the next step
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": "Continue to the next step."})

    return "Max steps reached — " + full_reasoning[-1]

result = react_loop(
    "A company has revenue of $10M and COGS of $4M. Calculate gross margin, "
    "estimate net margin assuming 30% operating expenses, and explain what "
    "these numbers suggest about the business's health."
)
print(f"\n[final] {result[:200]}")
```

**Expected Token Savings:** Each ReAct step is focused and short; the model self-monitors — "Thought" catches reasoning errors before they propagate; explicit observations make intermediate results verifiable.
**Environment:** Multi-step calculations, research tasks, decision trees where intermediate states matter.

---

### Option 3 — Hierarchical decomposition: task → subtasks → steps

```python
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SubTask:
    title:    str
    steps:    list[str] = field(default_factory=list)
    result:   str = ""
    complete: bool = False

@dataclass
class TaskPlan:
    goal:     str
    subtasks: list[SubTask] = field(default_factory=list)

def decompose(goal: str) -> TaskPlan:
    """Level 1: decompose goal into subtasks with step lists."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Decompose this goal into subtasks, each with 2-3 concrete steps.\n\n"
                f"Return JSON: [{{\"title\": \"\", \"steps\": [\"step1\", \"step2\"]}}]\n\n"
                f"Goal: {goal}"
            ),
        }],
    )
    try:
        raw = json.loads(response.content[0].text)
        subtasks = [SubTask(title=s["title"], steps=s.get("steps", [])) for s in raw]
    except (json.JSONDecodeError, KeyError):
        subtasks = [SubTask(title="Complete goal", steps=["Execute the task"])]
    return TaskPlan(goal=goal, subtasks=subtasks)

def execute_subtask(subtask: SubTask, context: str) -> str:
    """Level 2: execute a single subtask following its steps."""
    steps_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(subtask.steps))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Complete this subtask by following the steps exactly.\n\n"
                f"Subtask: {subtask.title}\n"
                f"Steps:\n{steps_str}\n\n"
                f"{'Context from prior subtasks:\n' + context if context else ''}\n\n"
                f"Be specific and produce actionable output."
            ),
        }],
    )
    return response.content[0].text.strip()

def run_hierarchical(goal: str) -> str:
    plan = decompose(goal)
    print(f"[plan] {len(plan.subtasks)} subtasks:")
    for i, st in enumerate(plan.subtasks, 1):
        print(f"  {i}. {st.title} ({len(st.steps)} steps)")

    context = ""
    for i, subtask in enumerate(plan.subtasks, 1):
        print(f"\n[exec] subtask {i}: {subtask.title}")
        result = execute_subtask(subtask, context)
        subtask.result = result
        subtask.complete = True
        context += f"\n[{subtask.title}]: {result[:300]}"
        print(f"  → {result[:100]}...")

    # Final synthesis
    synthesis = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Synthesise these subtask results into a coherent final answer for: {goal}\n\n"
                + "\n\n".join(f"{st.title}:\n{st.result}" for st in plan.subtasks)
            ),
        }],
    )
    return synthesis.content[0].text

result = run_hierarchical(
    "Write a competitive analysis of three Python web frameworks: Django, FastAPI, Flask. "
    "Include performance, ecosystem, and when to choose each."
)
print(f"\n[synthesis] {result[:300]}")
```

**Expected Token Savings:** Hierarchical decomposition keeps each API call short and focused; synthesis is cheap (summarising concrete outputs) vs attempting to write the whole analysis in one pass.
**Environment:** Document generation, competitive analysis, technical reports — any task with identifiable sections.

---

### Option 4 — Dependency-aware subtask graph

```python
import anthropic
import json
from collections import defaultdict, deque

client = anthropic.Anthropic()

def build_dag(task: str) -> list[dict]:
    """Ask Claude to produce a DAG of subtasks with dependencies."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Decompose this task into subtasks with dependencies.\n\n"
                f"Return JSON array where each item has:\n"
                f"  id: unique string (e.g. 'gather_data')\n"
                f"  title: description\n"
                f"  depends_on: list of ids (empty [] if no deps)\n\n"
                f"Task: {task}"
            ),
        }],
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return [{"id": "main", "title": task, "depends_on": []}]

def topological_sort(nodes: list[dict]) -> list[dict]:
    """Return nodes in topological order (deps before dependents)."""
    graph = {n["id"]: n for n in nodes}
    in_degree = defaultdict(int)
    for node in nodes:
        for dep in node.get("depends_on", []):
            in_degree[node["id"]] += 1

    queue = deque([n for n in nodes if in_degree[n["id"]] == 0])
    order = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for other in nodes:
            if node["id"] in other.get("depends_on", []):
                in_degree[other["id"]] -= 1
                if in_degree[other["id"]] == 0:
                    queue.append(other)
    return order

def execute_dag(task: str) -> dict[str, str]:
    nodes = build_dag(task)
    order = topological_sort(nodes)
    print(f"[dag] execution order: {[n['id'] for n in order]}")

    results: dict[str, str] = {}
    for node in order:
        # Build context from dependency outputs
        dep_context = "\n".join(
            f"[{dep}]: {results[dep][:200]}"
            for dep in node.get("depends_on", [])
            if dep in results
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Overall goal: {task}\n\n"
                    f"{'Dependencies completed:\n' + dep_context + chr(10) if dep_context else ''}"
                    f"Current subtask: {node['title']}\n\n"
                    f"Complete only this subtask."
                ),
            }],
        )
        results[node["id"]] = response.content[0].text.strip()
        print(f"[dag] {node['id']} ✓: {results[node['id']][:60]}")

    return results

outputs = execute_dag(
    "Create an onboarding plan for a new Python developer: assess skill level, "
    "select learning resources, schedule milestones, and write a 30-day goal checklist."
)
print(f"\n[dag] all outputs: {list(outputs.keys())}")
```

**Expected Token Savings:** Dependency-aware ordering ensures each subtask has exactly the context it needs — no wasted tokens passing irrelevant prior results; parallel-safe subtasks (same in-degree) could run concurrently.
**Environment:** Complex pipelines where subtasks have genuine data dependencies; project planning, multi-phase research.

---

### Option 5 — Subtask verification gate before proceeding

```python
import anthropic

client = anthropic.Anthropic()

def execute_with_verification(
    task: str,
    subtasks: list[str],
    verify: bool = True,
) -> list[dict]:
    """
    Execute each subtask; optionally run a verification step before
    advancing to the next — catches errors early.
    """
    results = []
    accumulated = ""

    for i, subtask in enumerate(subtasks, 1):
        # Execute subtask
        exec_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Task: {task}\n"
                    f"{'Context so far:\n' + accumulated + chr(10) if accumulated else ''}"
                    f"Subtask {i}/{len(subtasks)}: {subtask}\n\n"
                    f"Complete this subtask only. Be specific."
                ),
            }],
        )
        output = exec_resp.content[0].text.strip()

        # Verification gate
        if verify:
            verify_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Does this output correctly complete the subtask?\n\n"
                        f"Subtask: {subtask}\n"
                        f"Output: {output}\n\n"
                        f"Reply: PASS or FAIL: [brief reason]"
                    ),
                }],
            )
            verdict = verify_resp.content[0].text.strip()
            passed = verdict.upper().startswith("PASS")
            print(f"[verify] subtask {i}: {'✓' if passed else '✗'} {verdict[:60]}")

            if not passed:
                # Retry once with the verification feedback
                retry_resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Your previous attempt at subtask '{subtask}' failed: {verdict}\n\n"
                            f"Retry and fix the issue. Be specific and complete."
                        ),
                    }],
                )
                output = retry_resp.content[0].text.strip()
                print(f"[retry] subtask {i} retried")
        else:
            print(f"[execute] subtask {i}/{len(subtasks)}: {output[:60]}")

        results.append({"subtask": subtask, "output": output})
        accumulated += f"\n[{i}] {output[:200]}"

    return results

subtasks = [
    "List three key metrics for measuring API performance",
    "Explain why each metric matters for user experience",
    "Suggest one tool for monitoring each metric",
]

results = execute_with_verification(
    "Create a guide for API performance monitoring",
    subtasks,
    verify=True,
)
print(f"\n[done] {len(results)} subtasks completed")
for r in results:
    print(f"  → {r['output'][:80]}")
```

**Expected Token Savings:** Verification catches a bad subtask output before it propagates as bad context into subsequent subtasks — preventing compounding errors that would require re-running the whole chain; the verification call costs ~50 tokens vs re-running 3 subtasks.
**Environment:** High-stakes pipelines (financial analysis, medical summarisation); any task where one bad subtask output corrupts all subsequent steps.

---

### Option 6 — Tool-based subtask orchestration

```python
import anthropic
import json

client = anthropic.Anthropic()

# Subtask state machine — tools allow the agent to orchestrate itself
_subtask_queue: list[str] = []
_subtask_results: dict[str, str] = {}
_current_idx = 0

def add_subtask(title: str) -> dict:
    _subtask_queue.append(title)
    return {"added": title, "queue_length": len(_subtask_queue), "subtask_id": len(_subtask_queue) - 1}

def complete_subtask(subtask_id: int, result: str) -> dict:
    global _current_idx
    if subtask_id < len(_subtask_queue):
        _subtask_results[_subtask_queue[subtask_id]] = result
        _current_idx = subtask_id + 1
        remaining = len(_subtask_queue) - _current_idx
        return {"status": "completed", "subtask_id": subtask_id, "remaining": remaining}
    return {"status": "invalid_id"}

def get_next_subtask() -> dict:
    if _current_idx < len(_subtask_queue):
        return {"subtask_id": _current_idx, "title": _subtask_queue[_current_idx]}
    return {"status": "all_subtasks_complete", "results": _subtask_results}

tools = [
    {"name": "add_subtask",
     "description": "Add a subtask to the execution queue. Call this to plan all subtasks first.",
     "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}},
    {"name": "complete_subtask",
     "description": "Mark the current subtask as complete with its result.",
     "input_schema": {"type": "object", "properties": {
         "subtask_id": {"type": "integer"}, "result": {"type": "string"}}, "required": ["subtask_id", "result"]}},
    {"name": "get_next_subtask",
     "description": "Get the next pending subtask to work on.",
     "input_schema": {"type": "object", "properties": {}}},
]

DISPATCH = {
    "add_subtask":      lambda i: add_subtask(i["title"]),
    "complete_subtask": lambda i: complete_subtask(i["subtask_id"], i["result"]),
    "get_next_subtask": lambda i: get_next_subtask(),
}

def agent_loop(task: str) -> dict:
    messages = [{
        "role": "user",
        "content": (
            f"Complete this task by: 1) using add_subtask to plan all steps, "
            f"2) calling get_next_subtask, 3) completing each subtask with complete_subtask.\n\n"
            f"Task: {task}"
        ),
    }]

    for _ in range(30):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text[:100]}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = DISPATCH[block.name](block.input)
                print(f"[tool] {block.name} → {json.dumps(result)[:80]}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                      "content": json.dumps(result)})
        messages.append({"role": "user", "content": tool_results})

    return _subtask_results

results = agent_loop("Write a 3-step guide to setting up a Python virtual environment.")
print(f"\n[results] {json.dumps(results, indent=2)[:400]}")
```

**Expected Token Savings:** Tool-based orchestration makes subtask planning and execution explicit and inspectable; the agent self-documents its decomposition, making it easy to identify where it spent tokens and why.
**Environment:** Agentic frameworks that already use tool calling; agents that need to produce a structured, auditable work log alongside their output.

---

## Comparison

| Option | Decomposition Method | Verification | Dependency Tracking | Self-Directed | Best For |
|---|---|---|---|---|---|
| 1. Plan-then-execute | Prompt-generated list | No | No | No | Simple linear tasks; quickest to implement |
| 2. ReAct loop | Implicit (Thought/Act/Obs) | Via Observation | Implicit | Yes | Reasoning-heavy tasks; self-correcting |
| 3. Hierarchical | Two-level (subtask+steps) | No | No | No | Document generation; known structure |
| 4. DAG execution | Dependency graph | No | Yes | No | Pipelines with data dependencies |
| 5. Verification gate | Post-subtask check | Yes | No | No | High-stakes tasks; error propagation prevention |
| 6. Tool orchestration | Tool state machine | Implicit | Via queue | Yes | Auditable agentic pipelines; structured logging |
