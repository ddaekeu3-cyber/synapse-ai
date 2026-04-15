---
layout: solution
title: "Agent Reruns Completed Steps After Context Compaction"
category: token-cost
description: "After context compaction removes early conversation turns, the agent loses track of which steps it already completed and redoes them — doubling API calls, tool executions, and token spend for work that was already done."
tags: [token-cost, context-compaction, state-tracking, idempotency, long-running]
---

## Symptom

An agent is midway through a 12-step data pipeline. Context compaction fires at step 8, summarising the history. The summary loses the detail of which steps ran. The agent re-examines its task and restarts from step 1:

```
[Step 1] fetch_data(source="A")          → 1,200 tokens
[Step 2] transform(data)                 → 800 tokens
...
[Step 8] validate(results)               → 600 tokens
--- CONTEXT COMPACTION ---
[Step 1] fetch_data(source="A")  ← REDO  → 1,200 tokens (wasted)
[Step 2] transform(data)         ← REDO  → 800 tokens  (wasted)
...
```

Each re-run also has real side effects: files overwritten, emails re-sent, API quotas consumed.

## Root Cause

Progress is stored only in the conversation history. When compaction summarises history, step-completion details are lost or ambiguously stated:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: progress only in conversation, no durable state
messages = [
    {"role": "user", "content": "Run the 10-step data pipeline"},
    {"role": "assistant", "content": "Step 1 complete. Step 2 complete..."},
    # ... compacted to: "The agent completed several pipeline steps"
    # Agent re-reads task → doesn't know which steps ran → starts over
]
```

---

## Fix

### Option 1 — Explicit step tracking in a durable state file

Write step completion to a file after each step. On startup (and after compaction), read the file to determine where to resume.

```python
import anthropic
import json
import os
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")

STATE_FILE = Path("/tmp/pipeline_state.json")


def load_state() -> dict:
    """Load durable step state — survives context compaction."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed_steps": [], "results": {}, "pipeline_id": None}


def save_step(step_name: str, result: dict) -> None:
    """Mark a step as complete — write to durable state immediately."""
    state = load_state()
    if step_name not in state["completed_steps"]:
        state["completed_steps"].append(step_name)
    state["results"][step_name] = result
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"[state] Saved: {step_name} ✓")


def is_done(step_name: str) -> bool:
    """Check durable state — NOT conversation history."""
    state = load_state()
    return step_name in state["completed_steps"]


def get_result(step_name: str) -> dict | None:
    state = load_state()
    return state["results"].get(step_name)


# Pipeline steps (each idempotent)
PIPELINE_STEPS = [
    ("fetch_raw_data", lambda: {"rows": 5000, "source": "db_prod"}),
    ("validate_schema", lambda: {"valid": True, "errors": 0}),
    ("transform_data", lambda: {"rows_out": 4987, "nulls_dropped": 13}),
    ("compute_metrics", lambda: {"avg": 42.7, "p95": 98.1}),
    ("write_report", lambda: {"file": "report_2026Q1.pdf", "pages": 12}),
]


def run_pipeline_with_state() -> dict:
    """Run pipeline, skipping already-completed steps."""
    state = load_state()
    completed = state["completed_steps"]

    if completed:
        print(f"[resume] Resuming from after step: {completed[-1]}")

    for step_name, step_fn in PIPELINE_STEPS:
        if is_done(step_name):
            result = get_result(step_name)
            print(f"[skip] {step_name} already done: {result}")
            continue

        print(f"[run] Executing: {step_name}")
        result = step_fn()
        save_step(step_name, result)

    return load_state()


def generate_progress_summary() -> str:
    """Generate a compact progress summary to inject into context after compaction."""
    state = load_state()
    completed = state["completed_steps"]
    pending = [s for s, _ in PIPELINE_STEPS if s not in completed]

    return f"""## Pipeline Progress (authoritative — from durable state file)
Completed steps ({len(completed)}): {', '.join(completed) or 'none'}
Pending steps ({len(pending)}): {', '.join(pending) or 'none'}
Results: {json.dumps(state['results'], indent=2)}

IMPORTANT: Do NOT re-run completed steps. Resume from the first pending step."""


# Clear state for demo
STATE_FILE.unlink(missing_ok=True)

# Simulate running partway and then "compaction"
for step_name, step_fn in PIPELINE_STEPS[:3]:
    save_step(step_name, step_fn())

print("\n--- Simulated context compaction ---\n")
print(generate_progress_summary())
print()

# Resume — skips completed steps
final = run_pipeline_with_state()
print(f"\nPipeline complete: {len(final['completed_steps'])} steps")

# Expected Token Savings: skipping 3 re-done steps saves ~3,000 tokens + tool execution costs
# Environment: long-running pipelines; multi-step agents prone to context compaction
```

---

### Option 2 — Inject progress context at the start of every model turn

Before each model call, prepend a compact, authoritative progress summary derived from durable state — not from conversation history.

```python
import anthropic
import json
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")

PROGRESS_FILE = Path("/tmp/agent_progress.json")


class DurableProgress:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self._data = self._load()

    def _load(self) -> dict:
        if PROGRESS_FILE.exists():
            data = json.loads(PROGRESS_FILE.read_text())
            if data.get("task_id") == self.task_id:
                return data
        return {"task_id": self.task_id, "done": [], "outputs": {}}

    def _save(self) -> None:
        PROGRESS_FILE.write_text(json.dumps(self._data, indent=2))

    def mark_done(self, step: str, output: dict) -> None:
        if step not in self._data["done"]:
            self._data["done"].append(step)
        self._data["outputs"][step] = output
        self._save()

    def is_done(self, step: str) -> bool:
        return step in self._data["done"]

    def as_context_block(self) -> str:
        done = self._data["done"]
        outputs = self._data["outputs"]
        if not done:
            return "## Progress: No steps completed yet."
        lines = ["## Completed Steps (do not repeat these):"]
        for step in done:
            out = outputs.get(step, {})
            lines.append(f"- {step}: {json.dumps(out)}")
        return "\n".join(lines)


def run_with_injected_progress(task_id: str, steps: list[dict]) -> list[dict]:
    """
    Run a multi-step agentic task.
    Inject durable progress as a system suffix on every turn so compaction cannot erase it.
    """
    progress = DurableProgress(task_id)
    results = []

    for step in steps:
        name = step["name"]

        if progress.is_done(name):
            print(f"[skip] Already done: {name}")
            results.append({"step": name, "status": "skipped", "output": progress._data["outputs"][name]})
            continue

        # Build message with authoritative progress context
        progress_context = progress.as_context_block()
        system = f"""You are executing a multi-step task.
{progress_context}

RULE: Never re-execute a step listed as completed above.
Current step to execute: {name}
Step description: {step['description']}"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": f"Execute step: {name}"}]
        )

        output = {"model_response": response.content[0].text.strip(), "step": name}
        progress.mark_done(name, output)
        results.append({"step": name, "status": "done", "output": output})
        print(f"[done] {name}")

    return results


PROGRESS_FILE.unlink(missing_ok=True)

steps = [
    {"name": "load_config", "description": "Load and validate the pipeline configuration"},
    {"name": "fetch_data", "description": "Fetch raw data from the API"},
    {"name": "process_data", "description": "Apply transformations to the raw data"},
    {"name": "generate_report", "description": "Produce a summary report"},
]

results = run_with_injected_progress("pipeline_2026Q1", steps)
print(f"\nResults: {len([r for r in results if r['status'] == 'done'])} new, {len([r for r in results if r['status'] == 'skipped'])} skipped")

# Expected Token Savings: injected progress prevents redundant re-runs regardless of compaction depth
# Environment: agents running inside systems with automatic context compaction
```

---

### Option 3 — Step completion as structured tool results in history

Store completed steps as tool results rather than assistant text. Tool results are more likely to survive compaction summarisation.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# A fake "progress_tracker" tool whose results encode completed steps
PROGRESS_TOOL = {
    "name": "record_step_complete",
    "description": "Record that a pipeline step has been completed. Call after every step.",
    "input_schema": {
        "type": "object",
        "properties": {
            "step_name": {"type": "string"},
            "output_summary": {"type": "string"},
            "next_step": {"type": "string"}
        },
        "required": ["step_name", "output_summary"]
    }
}

STEP_TOOL = {
    "name": "execute_step",
    "description": "Execute a named pipeline step",
    "input_schema": {
        "type": "object",
        "properties": {"step_name": {"type": "string"}},
        "required": ["step_name"]
    }
}

completed_steps: dict[str, str] = {}  # step_name → summary


def handle_tool_call(name: str, input_data: dict) -> str:
    if name == "record_step_complete":
        step = input_data["step_name"]
        summary = input_data["output_summary"]
        completed_steps[step] = summary
        print(f"[recorded] ✓ {step}: {summary}")
        return json.dumps({"recorded": True, "step": step, "total_completed": len(completed_steps)})

    elif name == "execute_step":
        step = input_data["step_name"]
        if step in completed_steps:
            return json.dumps({"skip": True, "already_done": step, "prior_output": completed_steps[step]})
        # Simulate execution
        return json.dumps({"executed": True, "step": step, "result": f"Output of {step}"})

    return json.dumps({"error": "Unknown tool"})


def run_tracked_pipeline(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    system = """Execute pipeline steps one by one.
After completing each step, call record_step_complete immediately.
Before executing a step, call execute_step — it will tell you if already done.
Never re-execute a step that has already been recorded as complete."""

    tools = [PROGRESS_TOOL, STEP_TOOL]

    for _ in range(20):  # Max turns
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next((b.text for b in response.content if b.type == "text"), "Done")

        tool_results = []
        for tu in tool_uses:
            result = handle_tool_call(tu.name, tu.input)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Max turns reached"


result = run_tracked_pipeline("Run a 4-step data pipeline: extract, transform, validate, load")
print(f"\nFinal: {result}")
print(f"Completed steps: {list(completed_steps.keys())}")

# Expected Token Savings: tool-result encoding of progress is harder to lose in compaction summaries
# Environment: agents using structured tool calls for pipeline orchestration
```

---

### Option 4 — Checkpoint header: prepend completed-step manifest to every request

Maintain a running list of completed steps outside the main conversation. Prepend it as a cache-friendly system prompt block.

```python
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class CheckpointManager:
    """Maintain a completed-step manifest outside conversation history."""
    task_id: str
    completed: list[str] = field(default_factory=list)
    outputs: dict[str, dict] = field(default_factory=dict)

    def mark(self, step: str, output: dict) -> None:
        if step not in self.completed:
            self.completed.append(step)
        self.outputs[step] = output

    def as_system_block(self) -> str:
        if not self.completed:
            return ""
        manifest = "\n".join(
            f"  ✓ {step}: {json.dumps(self.outputs.get(step, {}))}"
            for step in self.completed
        )
        return f"""<completed_steps>
Task: {self.task_id}
The following steps are DONE. Do NOT re-run them under any circumstances:
{manifest}
</completed_steps>"""


def run_with_checkpoint(task_id: str, all_steps: list[str]) -> dict:
    chk = CheckpointManager(task_id=task_id)
    messages = []

    for step in all_steps:
        if step in chk.completed:
            print(f"[chk] Skip: {step}")
            continue

        # Build system with checkpoint manifest (prompt-cached block)
        checkpoint_block = chk.as_system_block()
        system = f"""{checkpoint_block}

You are executing task '{task_id}'.
Pending steps: {[s for s in all_steps if s not in chk.completed]}
RULE: Do NOT repeat any step listed in <completed_steps>."""

        messages.append({
            "role": "user",
            "content": f"Execute step: {step}"
        })

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=messages[-4:],  # Keep last 4 turns, checkpoint handles older context
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
        )

        result_text = response.content[0].text.strip()
        chk.mark(step, {"summary": result_text[:100]})
        print(f"[done] {step}")

        messages.append({"role": "assistant", "content": response.content})

    return {"completed": chk.completed, "outputs": chk.outputs}


steps = ["parse_config", "validate_inputs", "fetch_records", "apply_rules", "export_output"]
result = run_with_checkpoint("etl_job_042", steps)
print(f"\nDone: {result['completed']}")

# Expected Token Savings: checkpoint block is cache-eligible; compaction cannot remove it from system prompt
# Environment: agents with explicit system prompt control; long multi-step tasks
```

---

### Option 5 — Step deduplication via operation log with hash

Log each executed operation with a content hash. Before re-running, check the log to see if identical inputs have already been processed.

```python
import anthropic
import json
import hashlib
import time
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")

OP_LOG = Path("/tmp/operation_log.jsonl")


def operation_hash(step_name: str, input_data: dict) -> str:
    """Deterministic hash of step + inputs — same operation always produces same hash."""
    canonical = json.dumps({"step": step_name, **input_data}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def log_operation(step_name: str, input_data: dict, output: dict) -> str:
    """Write completed operation to append-only log. Returns operation hash."""
    op_hash = operation_hash(step_name, input_data)
    entry = {
        "hash": op_hash,
        "step": step_name,
        "input": input_data,
        "output": output,
        "ts": time.time()
    }
    with OP_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return op_hash


def find_prior_result(step_name: str, input_data: dict) -> dict | None:
    """Check operation log for identical prior execution."""
    if not OP_LOG.exists():
        return None
    target_hash = operation_hash(step_name, input_data)
    for line in OP_LOG.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("hash") == target_hash:
            return entry["output"]
    return None


def execute_step(step_name: str, input_data: dict) -> dict:
    """Execute step only if not previously completed with identical inputs."""
    prior = find_prior_result(step_name, input_data)
    if prior is not None:
        print(f"[dedup] ✓ {step_name} (same inputs run before) → cached output")
        return {**prior, "_from_log": True}

    # Execute fresh
    print(f"[exec] Running: {step_name}")
    output = {"step": step_name, "result": f"output_of_{step_name}", "ts": time.time()}
    op_hash = log_operation(step_name, input_data, output)
    print(f"[log] Recorded {step_name} (hash={op_hash})")
    return output


def generate_op_log_context() -> str:
    """Produce a summary of the op log to inject after compaction."""
    if not OP_LOG.exists():
        return "No operations logged yet."
    entries = [json.loads(l) for l in OP_LOG.read_text().splitlines() if l.strip()]
    if not entries:
        return "Operation log is empty."
    lines = ["## Operation Log (do not re-run these):"]
    for e in entries:
        lines.append(f"- {e['step']} (hash={e['hash']}): {json.dumps(e['output'])[:80]}")
    return "\n".join(lines)


# Clear log for demo
OP_LOG.unlink(missing_ok=True)

# First run
for step, inputs in [("fetch", {"source": "api_v2"}), ("transform", {"mode": "clean"}), ("export", {"format": "csv"})]:
    execute_step(step, inputs)

print("\n--- After context compaction ---")
print(generate_op_log_context())
print()

# Re-run with same inputs — all deduplicated
for step, inputs in [("fetch", {"source": "api_v2"}), ("transform", {"mode": "clean"}), ("export", {"format": "csv"})]:
    execute_step(step, inputs)

# Expected Token Savings: content-hash dedup catches re-runs even if step names differ slightly
# Environment: data pipeline agents; ETL orchestration; batch processing with restart risk
```

---

### Option 6 — React-style state machine: agent can only transition forward

Model the pipeline as an explicit state machine. The agent can only call `advance_state(next_step)` — it cannot go backward or skip.

```python
import anthropic
import json
from enum import StrEnum

client = anthropic.Anthropic(api_key="sk-live-...")


class PipelineState(StrEnum):
    INIT = "init"
    FETCHING = "fetching"
    TRANSFORMING = "transforming"
    VALIDATING = "validating"
    EXPORTING = "exporting"
    COMPLETE = "complete"
    FAILED = "failed"


VALID_TRANSITIONS = {
    PipelineState.INIT: [PipelineState.FETCHING],
    PipelineState.FETCHING: [PipelineState.TRANSFORMING, PipelineState.FAILED],
    PipelineState.TRANSFORMING: [PipelineState.VALIDATING, PipelineState.FAILED],
    PipelineState.VALIDATING: [PipelineState.EXPORTING, PipelineState.FAILED],
    PipelineState.EXPORTING: [PipelineState.COMPLETE, PipelineState.FAILED],
    PipelineState.COMPLETE: [],
    PipelineState.FAILED: [],
}

current_state = PipelineState.INIT
state_outputs: dict[str, dict] = {}


def advance_state(next_state: str, output: dict) -> dict:
    global current_state
    try:
        target = PipelineState(next_state)
    except ValueError:
        return {"error": f"Unknown state: {next_state}"}

    if target not in VALID_TRANSITIONS.get(current_state, []):
        return {
            "error": f"Invalid transition: {current_state} → {target}",
            "current_state": current_state,
            "valid_next": [s.value for s in VALID_TRANSITIONS.get(current_state, [])],
            "note": "You cannot skip or go backward. Advance to the next valid state."
        }

    current_state = target
    state_outputs[target.value] = output
    print(f"[state] Transitioned to: {current_state}")
    return {"new_state": current_state, "output_recorded": True}


def get_status() -> dict:
    return {
        "current_state": current_state,
        "completed_states": list(state_outputs.keys()),
        "outputs": state_outputs,
        "next_valid": [s.value for s in VALID_TRANSITIONS.get(current_state, [])]
    }


tools = [
    {
        "name": "advance_state",
        "description": "Advance the pipeline to the next state. You can only move forward — never backward.",
        "input_schema": {
            "type": "object",
            "properties": {
                "next_state": {"type": "string", "enum": [s.value for s in PipelineState]},
                "output": {"type": "object", "description": "Summary of what was accomplished in this step"}
            },
            "required": ["next_state", "output"]
        }
    },
    {
        "name": "get_status",
        "description": "Get current pipeline state and completed steps",
        "input_schema": {"type": "object", "properties": {}}
    }
]


def run_state_machine_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    system = f"""You are a pipeline orchestrator using a state machine.
Current state: {current_state}
You advance states using advance_state(). You cannot go backward or skip states.
Always check get_status() first to see where you are."""

    for _ in range(15):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next((b.text for b in response.content if b.type == "text"), "Done")

        tool_results = []
        for tu in tool_uses:
            if tu.name == "advance_state":
                result = advance_state(**tu.input)
            elif tu.name == "get_status":
                result = get_status()
            else:
                result = {"error": "Unknown tool"}
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Max turns reached"


result = run_state_machine_agent("Run the full data pipeline from init to complete")
print(f"\n{result}")
print(f"Final state: {current_state}")

# Expected Token Savings: state machine prevents backward movement → zero re-runs structurally
# Environment: multi-step pipelines where step ordering and idempotency are critical
```

---

## Comparison

| Option | Re-run Prevention | Survives Restart | Survives Compaction | Complexity |
|--------|-----------------|-----------------|---------------------|------------|
| 1 | File-based state | Yes | Yes | Low |
| 2 | Injected progress | No (in-memory) | Yes (injected each turn) | Low |
| 3 | Tool result encoding | Partial | Partial | Low |
| 4 | Checkpoint header | No (in-memory) | Yes (system prompt) | Medium |
| 5 | Content-hash op log | Yes | Yes | Medium |
| 6 | State machine | Structural | Partial | Medium |

**Recommended starting point:** Option 1 (durable state file) — write step completion to a JSON file immediately after each step succeeds, and read it at the top of the pipeline function. This costs two file I/O operations per step and completely prevents re-runs regardless of compaction, restarts, or context loss. Combine with Option 2 to also inject a progress summary into the system context as a belt-and-suspenders guard.
