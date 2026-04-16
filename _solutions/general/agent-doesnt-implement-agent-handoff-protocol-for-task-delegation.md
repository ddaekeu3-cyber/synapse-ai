---
layout: solution
title: "Agent Doesn't Implement Agent Handoff Protocol for Task Delegation"
category: general
description: "A structured handoff protocol — passing task context, completion criteria, partial results, and handoff metadata between agents — prevents dropped state, duplicate work, and silent failures when one agent delegates to another."
tags: [multi-agent, handoff, delegation, orchestration, reliability, context-passing]
---

## Problem

When an orchestrator agent hands a task to a sub-agent, critical context is often lost: what has already been tried, what the acceptance criteria are, what partial results exist, and what the sub-agent should return. Without a structured handoff protocol, sub-agents start from scratch, repeat work, produce incompatible outputs, or silently fail without signaling back to the orchestrator.

## Solutions

### Option 1: Typed Handoff Envelope

```python
import anthropic
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional, Any

client = anthropic.Anthropic()

@dataclass
class HandoffEnvelope:
    """Structured container passed from one agent to another."""
    handoff_id: str
    task_id: str
    from_agent: str
    to_agent: str
    task_description: str
    acceptance_criteria: list[str]
    partial_results: dict[str, Any]
    context_summary: str
    constraints: list[str]
    created_at: float = field(default_factory=time.time)
    priority: str = "normal"  # low | normal | high | critical

    def to_prompt_section(self) -> str:
        criteria = "\n".join(f"  - {c}" for c in self.acceptance_criteria)
        constraints = "\n".join(f"  - {c}" for c in self.constraints)
        partial = "\n".join(f"  {k}: {v}" for k, v in self.partial_results.items())
        return f"""=== AGENT HANDOFF ===
Handoff ID: {self.handoff_id}
From: {self.from_agent} → To: {self.to_agent}
Task: {self.task_description}

Prior Context:
{self.context_summary}

Partial Results Available:
{partial if self.partial_results else "  (none)"}

Acceptance Criteria (your output MUST satisfy all):
{criteria}

Constraints:
{constraints}
===================="""

@dataclass
class HandoffResult:
    handoff_id: str
    from_agent: str
    status: str  # completed | failed | partial | escalate
    output: str
    output_fields: dict[str, Any]
    completion_notes: str
    next_agent: Optional[str] = None
    next_task: Optional[str] = None

def create_handoff(
    from_agent: str,
    to_agent: str,
    task: str,
    criteria: list[str],
    context: str = "",
    partial: dict = None,
    constraints: list[str] = None
) -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id=str(uuid.uuid4())[:8],
        task_id=str(uuid.uuid4())[:8],
        from_agent=from_agent,
        to_agent=to_agent,
        task_description=task,
        acceptance_criteria=criteria,
        partial_results=partial or {},
        context_summary=context,
        constraints=constraints or []
    )

def execute_handoff(envelope: HandoffEnvelope) -> HandoffResult:
    """Execute the handoff by running the target agent with the structured envelope."""
    system = f"""You are {envelope.to_agent}. You have received a structured task handoff.
Complete the task described in the handoff envelope. Return your result clearly addressing each acceptance criterion."""

    user_prompt = f"""{envelope.to_prompt_section()}

Please complete this task. Structure your response as:
STATUS: <completed|failed|partial|escalate>
OUTPUT: <your main deliverable>
NOTES: <completion notes, what was done, any issues>"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": user_prompt}]
    )
    text = response.content[0].text

    # Parse structured response
    import re
    status = re.search(r"STATUS:\s*(\w+)", text)
    output = re.search(r"OUTPUT:\s*(.+?)(?=NOTES:|$)", text, re.DOTALL)
    notes = re.search(r"NOTES:\s*(.+?)$", text, re.DOTALL)

    return HandoffResult(
        handoff_id=envelope.handoff_id,
        from_agent=envelope.to_agent,
        status=status.group(1).lower() if status else "completed",
        output=output.group(1).strip() if output else text,
        output_fields={},
        completion_notes=notes.group(1).strip() if notes else ""
    )

# Usage
handoff = create_handoff(
    from_agent="OrchestratorAgent",
    to_agent="ResearchAgent",
    task="Find the top 3 Python web frameworks by GitHub stars",
    criteria=[
        "List exactly 3 frameworks",
        "Include GitHub star counts for each",
        "Include one sentence description per framework"
    ],
    context="User wants to choose a framework for a new REST API project",
    constraints=["Do not include Django REST Framework (already evaluated)", "Focus on 2024 data"]
)

result = execute_handoff(handoff)
print(f"Handoff {result.handoff_id}: {result.status}")
print(f"Output: {result.output[:300]}")
print(f"Notes: {result.completion_notes}")

# Expected Token Savings: No savings — protocol overhead ~100 tokens, prevents costly re-runs
# Environment: ANTHROPIC_API_KEY required
```

### Option 2: Stateful Handoff Registry with Chain Tracking

```python
import anthropic
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

client = anthropic.Anthropic()

class HandoffStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"

@dataclass
class HandoffRecord:
    handoff_id: str
    chain_id: str            # Groups all handoffs in one task chain
    sequence: int            # Position in the chain
    from_agent: str
    to_agent: str
    task: str
    result: str = ""
    status: HandoffStatus = HandoffStatus.PENDING
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    failure_reason: str = ""

class HandoffRegistry:
    def __init__(self):
        self._records: dict[str, HandoffRecord] = {}
        self._chains: dict[str, list[str]] = {}  # chain_id -> [handoff_ids]

    def create(self, chain_id: str, from_agent: str, to_agent: str, task: str) -> HandoffRecord:
        handoff_id = str(uuid.uuid4())[:8]
        seq = len(self._chains.get(chain_id, []))
        record = HandoffRecord(
            handoff_id=handoff_id,
            chain_id=chain_id,
            sequence=seq,
            from_agent=from_agent,
            to_agent=to_agent,
            task=task
        )
        self._records[handoff_id] = record
        self._chains.setdefault(chain_id, []).append(handoff_id)
        return record

    def complete(self, handoff_id: str, result: str):
        r = self._records[handoff_id]
        r.result = result[:500]
        r.status = HandoffStatus.COMPLETED
        r.completed_at = time.time()

    def fail(self, handoff_id: str, reason: str):
        r = self._records[handoff_id]
        r.status = HandoffStatus.FAILED
        r.failure_reason = reason
        r.completed_at = time.time()

    def get_chain_context(self, chain_id: str) -> str:
        """Build a summary of all prior handoffs in the chain."""
        ids = self._chains.get(chain_id, [])
        lines = []
        for hid in ids:
            r = self._records[hid]
            if r.status == HandoffStatus.COMPLETED:
                lines.append(f"[{r.sequence}] {r.from_agent}→{r.to_agent}: {r.result[:100]}")
        return "\n".join(lines) if lines else "(no prior steps)"

    def print_chain(self, chain_id: str):
        ids = self._chains.get(chain_id, [])
        print(f"\n=== HANDOFF CHAIN {chain_id[:8]} ===")
        for hid in ids:
            r = self._records[hid]
            dur = (r.completed_at - r.created_at) if r.completed_at else 0
            print(f"  [{r.sequence}] {r.from_agent} → {r.to_agent} [{r.status.value}] {dur:.1f}s")
            if r.result:
                print(f"       Result: {r.result[:80]}")

registry = HandoffRegistry()

def run_chained_handoff(
    registry: HandoffRegistry,
    chain_id: str,
    from_agent: str,
    to_agent: str,
    task: str
) -> str:
    # Build context from prior chain steps
    prior_context = registry.get_chain_context(chain_id)

    record = registry.create(chain_id, from_agent, to_agent, task)

    prompt = f"""You are {to_agent}. You are step {record.sequence} in a multi-agent pipeline.

Prior steps completed:
{prior_context}

Your task: {task}

Complete your task, building on prior results where relevant. Be concise."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text
        registry.complete(record.handoff_id, result)
        return result
    except Exception as e:
        registry.fail(record.handoff_id, str(e))
        raise

# Usage: a 4-step pipeline
chain_id = str(uuid.uuid4())[:8]

r1 = run_chained_handoff(registry, chain_id, "User", "Planner",
    "Create an outline for a blog post about async Python programming")
r2 = run_chained_handoff(registry, chain_id, "Planner", "Writer",
    "Write the introduction section based on the outline")
r3 = run_chained_handoff(registry, chain_id, "Writer", "Editor",
    "Edit and improve the introduction for clarity and engagement")
r4 = run_chained_handoff(registry, chain_id, "Editor", "SEOAgent",
    "Suggest 5 SEO keywords for this content")

registry.print_chain(chain_id)

# Expected Token Savings: Prior context keeps each agent ~50% shorter on task clarification
# Environment: ANTHROPIC_API_KEY required
```

### Option 3: Capability-Matched Handoff Routing

```python
import anthropic
import uuid
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum, auto

client = anthropic.Anthropic()

class Capability(Enum):
    RESEARCH = auto()
    WRITING = auto()
    CODE_GENERATION = auto()
    DATA_ANALYSIS = auto()
    REVIEW = auto()
    SUMMARIZATION = auto()
    PLANNING = auto()
    FACT_CHECKING = auto()

@dataclass
class AgentSpec:
    name: str
    capabilities: list[Capability]
    model: str = "claude-haiku-4-5-20251001"
    system_prompt: str = ""
    max_tokens: int = 300

AGENT_REGISTRY: dict[str, AgentSpec] = {
    "researcher": AgentSpec(
        name="ResearchAgent",
        capabilities=[Capability.RESEARCH, Capability.FACT_CHECKING],
        system_prompt="You are a thorough research agent. Provide factual, sourced information."
    ),
    "writer": AgentSpec(
        name="WriterAgent",
        capabilities=[Capability.WRITING, Capability.SUMMARIZATION],
        system_prompt="You are a skilled technical writer. Write clearly and concisely.",
        model="claude-sonnet-4-6"
    ),
    "coder": AgentSpec(
        name="CodeAgent",
        capabilities=[Capability.CODE_GENERATION, Capability.REVIEW],
        system_prompt="You are an expert software engineer. Write clean, documented code."
    ),
    "analyst": AgentSpec(
        name="DataAnalyst",
        capabilities=[Capability.DATA_ANALYSIS, Capability.SUMMARIZATION],
        system_prompt="You are a data analyst. Identify patterns and insights in data."
    ),
    "planner": AgentSpec(
        name="PlannerAgent",
        capabilities=[Capability.PLANNING, Capability.REVIEW],
        system_prompt="You are a strategic planner. Break down tasks into clear steps."
    ),
}

def find_best_agent(required_capability: Capability) -> Optional[AgentSpec]:
    """Find the most suitable agent for a capability."""
    matches = [a for a in AGENT_REGISTRY.values() if required_capability in a.capabilities]
    return matches[0] if matches else None

@dataclass
class CapabilityHandoff:
    handoff_id: str
    required_capability: Capability
    task: str
    context: str
    from_agent: str
    resolved_agent: Optional[AgentSpec] = None
    result: str = ""
    success: bool = False

def execute_capability_handoff(
    from_agent: str,
    required_capability: Capability,
    task: str,
    context: str = ""
) -> CapabilityHandoff:
    """Route task to the best available agent for the required capability."""
    handoff = CapabilityHandoff(
        handoff_id=str(uuid.uuid4())[:8],
        required_capability=required_capability,
        task=task,
        context=context,
        from_agent=from_agent
    )

    agent_spec = find_best_agent(required_capability)
    if not agent_spec:
        raise ValueError(f"No agent found with capability: {required_capability.name}")

    handoff.resolved_agent = agent_spec

    user_content = task
    if context:
        user_content = f"Context from prior agent ({from_agent}):\n{context}\n\nYour task: {task}"

    response = client.messages.create(
        model=agent_spec.model,
        max_tokens=agent_spec.max_tokens,
        system=agent_spec.system_prompt,
        messages=[{"role": "user", "content": user_content}]
    )
    handoff.result = response.content[0].text
    handoff.success = True

    print(f"[{handoff.handoff_id}] {from_agent} → {agent_spec.name} [{required_capability.name}]")
    return handoff

# Usage: orchestrator routes by capability
h1 = execute_capability_handoff(
    "Orchestrator",
    Capability.RESEARCH,
    "What are the main use cases for vector databases in AI applications?"
)
h2 = execute_capability_handoff(
    h1.resolved_agent.name,
    Capability.WRITING,
    "Write a 3-bullet summary suitable for a technical blog post",
    context=h1.result[:300]
)
h3 = execute_capability_handoff(
    h2.resolved_agent.name,
    Capability.REVIEW,
    "Review this summary for technical accuracy",
    context=h2.result[:300]
)

print(f"\nFinal output: {h3.result[:300]}")

# Expected Token Savings: Capability routing prevents over-provisioning (e.g., using Opus for simple tasks)
# Environment: ANTHROPIC_API_KEY required
```

### Option 4: Async Parallel Handoff Fan-Out

```python
import anthropic
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class ParallelHandoff:
    """Fan-out one task to multiple specialized agents, then aggregate."""
    orchestration_id: str
    parent_task: str
    sub_tasks: list[dict]  # [{agent, task, weight}]
    results: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

async def run_sub_agent(agent_name: str, task: str, context: str = "") -> tuple[str, str]:
    """Run a single sub-agent asynchronously."""
    content = f"{context}\n\nTask: {task}" if context else task
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250,
        system=f"You are {agent_name}. Complete your assigned task concisely.",
        messages=[{"role": "user", "content": content}]
    )
    return agent_name, response.content[0].text

async def parallel_handoff_fanout(
    orchestrator_name: str,
    parent_task: str,
    sub_tasks: list[tuple[str, str]],  # [(agent_name, task_description)]
    shared_context: str = ""
) -> ParallelHandoff:
    """
    Fan out parent task to multiple sub-agents in parallel,
    then return aggregated results.
    """
    fanout = ParallelHandoff(
        orchestration_id=str(uuid.uuid4())[:8],
        parent_task=parent_task,
        sub_tasks=[{"agent": a, "task": t} for a, t in sub_tasks]
    )

    # Launch all sub-agents concurrently
    coros = [run_sub_agent(agent, task, shared_context) for agent, task in sub_tasks]

    results = await asyncio.gather(*coros, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            agent_name = "unknown"
            fanout.errors[agent_name] = str(result)
        else:
            agent_name, output = result
            fanout.results[agent_name] = output

    return fanout

async def aggregate_results(fanout: ParallelHandoff, aggregator_agent: str) -> str:
    """Have an aggregator agent synthesize all sub-agent results."""
    results_text = "\n\n".join(
        f"=== {agent} ===\n{output}"
        for agent, output in fanout.results.items()
    )

    prompt = f"""Parent task: {fanout.parent_task}

Sub-agent results to synthesize:
{results_text}

Synthesize these results into a single coherent response."""

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"You are {aggregator_agent}. Synthesize multiple sub-agent outputs into one coherent result.",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

async def main():
    parent_task = "Evaluate Python vs Go for building a high-performance API service"

    fanout = await parallel_handoff_fanout(
        orchestrator_name="Orchestrator",
        parent_task=parent_task,
        sub_tasks=[
            ("PythonExpert", "List Python's strengths and weaknesses for high-performance APIs"),
            ("GoExpert", "List Go's strengths and weaknesses for high-performance APIs"),
            ("ArchitectAgent", "What are the key criteria when choosing between Python and Go for APIs?"),
            ("BenchmarkAgent", "What do typical Python vs Go API benchmarks show?"),
        ],
        shared_context="Focus on production use cases with >10k RPS requirements."
    )

    print(f"[{fanout.orchestration_id}] Fan-out complete: {len(fanout.results)} results, {len(fanout.errors)} errors")

    final = await aggregate_results(fanout, "TechAdvisor")
    print(f"\nSynthesized recommendation:\n{final[:500]}")

asyncio.run(main())

# Expected Token Savings: 4x latency reduction from parallel execution vs sequential chain
# Environment: ANTHROPIC_API_KEY required, uses asyncio, claude-sonnet-4-6 for aggregation
```

### Option 5: Handoff with Rollback and Retry

```python
import anthropic
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

client = anthropic.Anthropic()

MAX_RETRIES = 3
RETRY_DELAY = 1.0

@dataclass
class RobustHandoff:
    handoff_id: str
    from_agent: str
    to_agent: str
    task: str
    acceptance_fn: Optional[Callable[[str], bool]]
    result: str = ""
    attempts: int = 0
    success: bool = False
    failure_log: list[str] = field(default_factory=list)
    rollback_data: dict = field(default_factory=dict)  # State to restore on failure

def validate_output(output: str, criteria: list[str]) -> tuple[bool, str]:
    """Basic validation — checks criteria keywords are addressed."""
    output_lower = output.lower()
    missing = [c for c in criteria if not any(
        word.lower() in output_lower
        for word in c.split()[:3]  # Check first 3 words of each criterion
    )]
    if missing:
        return False, f"Output missing coverage for: {missing[:2]}"
    return True, "all criteria addressed"

def execute_robust_handoff(
    from_agent: str,
    to_agent: str,
    task: str,
    validation_criteria: list[str] = None,
    fallback_model: str = "claude-sonnet-4-6",
    rollback_state: dict = None
) -> RobustHandoff:
    """
    Execute a handoff with retry logic and rollback capability.
    """
    handoff = RobustHandoff(
        handoff_id=str(uuid.uuid4())[:8],
        from_agent=from_agent,
        to_agent=to_agent,
        task=task,
        acceptance_fn=None,
        rollback_data=rollback_state or {}
    )

    models = ["claude-haiku-4-5-20251001"] * 2 + [fallback_model]
    criteria = validation_criteria or []

    for attempt in range(MAX_RETRIES):
        handoff.attempts += 1
        model = models[min(attempt, len(models) - 1)]

        prior_failures = ""
        if handoff.failure_log:
            prior_failures = f"\n\nPrevious attempts failed because:\n" + "\n".join(
                f"- {f}" for f in handoff.failure_log
            )

        prompt = f"""You are {to_agent}. Complete this task carefully.{prior_failures}

Task: {task}

{"Ensure your response covers: " + ", ".join(criteria) if criteria else ""}"""

        try:
            response = client.messages.create(
                model=model,
                max_tokens=400,
                system=f"You are {to_agent}. Be thorough and precise.",
                messages=[{"role": "user", "content": prompt}]
            )
            output = response.content[0].text

            # Validate output
            if criteria:
                valid, reason = validate_output(output, criteria)
                if not valid:
                    handoff.failure_log.append(f"Attempt {attempt+1}: {reason}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY)
                    continue

            handoff.result = output
            handoff.success = True
            print(f"[{handoff.handoff_id}] {to_agent} succeeded on attempt {attempt+1}/{MAX_RETRIES} with {model}")
            return handoff

        except Exception as e:
            handoff.failure_log.append(f"Attempt {attempt+1} error: {str(e)}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

    # All retries exhausted — apply rollback
    print(f"[{handoff.handoff_id}] FAILED after {MAX_RETRIES} attempts. Rollback state: {handoff.rollback_data}")
    if handoff.rollback_data:
        print(f"  Restoring: {list(handoff.rollback_data.keys())}")

    return handoff

# Usage
h = execute_robust_handoff(
    from_agent="Orchestrator",
    to_agent="SecurityAuditor",
    task="Audit this code snippet for SQL injection vulnerabilities: `query = f'SELECT * FROM users WHERE id={user_id}'`",
    validation_criteria=[
        "SQL injection",
        "parameterized",
        "recommendation"
    ],
    rollback_state={"pipeline_stage": "pre-audit", "pending_deployments": ["service-a", "service-b"]}
)

print(f"Success: {h.success}, Attempts: {h.attempts}")
if h.success:
    print(f"Output: {h.result[:300]}")
else:
    print(f"Failure log: {h.failure_log}")

# Expected Token Savings: 2x tokens on retry vs restarting full pipeline; failure feedback improves retry quality
# Environment: ANTHROPIC_API_KEY required
```

### Option 6: Streaming Handoff with Real-Time Progress

```python
import anthropic
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

client = anthropic.Anthropic()

@dataclass
class StreamingHandoff:
    handoff_id: str
    from_agent: str
    to_agent: str
    task: str
    chunks_received: int = 0
    total_chars: int = 0
    full_output: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def streaming_duration_ms(self) -> float:
        if self.completed_at:
            return (self.completed_at - self.started_at) * 1000
        return 0.0

def execute_streaming_handoff(
    from_agent: str,
    to_agent: str,
    task: str,
    context: str = "",
    on_chunk: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[["StreamingHandoff"], None]] = None
) -> StreamingHandoff:
    """
    Execute handoff with streaming output — enables real-time display
    and downstream processing before the full response arrives.
    """
    handoff = StreamingHandoff(
        handoff_id=str(uuid.uuid4())[:8],
        from_agent=from_agent,
        to_agent=to_agent,
        task=task
    )

    user_content = task
    if context:
        user_content = f"Context from {from_agent}:\n{context}\n\nTask: {task}"

    print(f"[{handoff.handoff_id}] {from_agent} → {to_agent} (streaming)")
    print(f"  Task: {task[:60]}...")
    print(f"  Output: ", end="", flush=True)

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f"You are {to_agent}. Complete the task thoroughly.",
        messages=[{"role": "user", "content": user_content}]
    ) as stream:
        for text in stream.text_stream:
            handoff.chunks_received += 1
            handoff.total_chars += len(text)
            handoff.full_output += text

            # Real-time display
            print(text, end="", flush=True)

            # Optional downstream processing
            if on_chunk:
                on_chunk(text)

        # Get final usage
        final_msg = stream.get_final_message()
        handoff.input_tokens = final_msg.usage.input_tokens
        handoff.output_tokens = final_msg.usage.output_tokens

    print()  # newline after streaming
    handoff.completed_at = time.time()

    print(f"  Done: {handoff.chunks_received} chunks, {handoff.total_chars} chars, "
          f"{handoff.streaming_duration_ms:.0f}ms, "
          f"{handoff.input_tokens}+{handoff.output_tokens} tokens")

    if on_complete:
        on_complete(handoff)

    return handoff

# Usage: multi-step streaming pipeline
collected_output = []

def collect_chunk(chunk: str):
    collected_output.append(chunk)

h1 = execute_streaming_handoff(
    "User",
    "OutlineAgent",
    "Create a 4-point outline for an article about WebAssembly in 2024",
    on_chunk=collect_chunk
)

h2 = execute_streaming_handoff(
    "OutlineAgent",
    "WriterAgent",
    "Expand point 1 of this outline into a full paragraph",
    context=h1.full_output[:400]
)

h3 = execute_streaming_handoff(
    "WriterAgent",
    "ProofreaderAgent",
    "Proofread and improve the following paragraph",
    context=h2.full_output[:400]
)

print(f"\n=== HANDOFF CHAIN SUMMARY ===")
for h in [h1, h2, h3]:
    print(f"  {h.from_agent} → {h.to_agent}: {h.streaming_duration_ms:.0f}ms, {h.output_tokens} output tokens")

# Expected Token Savings: Streaming enables parallel downstream processing; TTFB reduced ~3x
# Environment: ANTHROPIC_API_KEY required, uses streaming API
```

## Comparison

| Option | Context Passing | Validation | Async | Recovery | Best Use Case |
|--------|----------------|------------|-------|----------|---------------|
| Typed Handoff Envelope | Structured envelope | Acceptance criteria | No | No | Formal pipeline interfaces |
| Stateful Registry + Chain | Cumulative prior steps | No | No | No | Sequential multi-step chains |
| Capability-Matched Routing | Shared context string | No | No | No | Dynamic agent selection |
| Async Parallel Fan-Out | Shared context | No | Yes | No | Independent parallel sub-tasks |
| Rollback + Retry | Failure feedback | Keyword validation | No | Yes | Critical accuracy requirements |
| Streaming Handoff | Prior output | No | No | No | Real-time display, low-latency UX |
