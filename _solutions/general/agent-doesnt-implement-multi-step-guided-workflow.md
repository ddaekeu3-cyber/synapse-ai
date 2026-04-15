---
layout: solution
title: "Agent Doesn't Implement Multi-Step Guided Workflow"
category: general
description: "Agent tries to complete complex tasks in one shot, producing poor results when the task requires sequential information gathering, user confirmation, or intermediate validation steps."
tags: [general, workflow, multi-step, wizard, guided, state-machine, ux]
---

# Agent Doesn't Implement Multi-Step Guided Workflow

## Problem

A user asks an agent to "create a deployment plan." The agent produces a generic plan because it doesn't know the tech stack, team size, timeline, or risk tolerance. Instead of asking clarifying questions in a structured sequence, it either asks everything at once (overwhelming) or proceeds blindly (wrong output). A guided multi-step workflow collects information progressively before acting.

---

## Option 1: Step State Machine with Explicit Transitions

Define workflow steps as states with transitions. The agent progresses through states, collecting information at each step before moving forward.

```python
import anthropic
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class WorkflowState(Enum):
    COLLECT_GOAL = "collect_goal"
    COLLECT_CONSTRAINTS = "collect_constraints"
    COLLECT_TECH_STACK = "collect_tech_stack"
    GENERATE_PLAN = "generate_plan"
    REVIEW_AND_CONFIRM = "review_and_confirm"
    DONE = "done"

@dataclass
class WorkflowContext:
    state: WorkflowState = WorkflowState.COLLECT_GOAL
    goal: str = ""
    constraints: str = ""
    tech_stack: str = ""
    draft_plan: str = ""
    confirmed: bool = False
    history: list[dict] = field(default_factory=list)

STATE_PROMPTS = {
    WorkflowState.COLLECT_GOAL: "What is the main goal of your deployment? (e.g., launch new feature, migrate DB, scale infra)",
    WorkflowState.COLLECT_CONSTRAINTS: "What are your constraints? (timeline, team size, budget, downtime tolerance)",
    WorkflowState.COLLECT_TECH_STACK: "What is your tech stack? (language, framework, cloud provider, CI/CD tool)",
    WorkflowState.GENERATE_PLAN: None,  # Auto-generated
    WorkflowState.REVIEW_AND_CONFIRM: "Does this plan look good? Reply 'yes' to finalize or describe what to change.",
    WorkflowState.DONE: None,
}

client = anthropic.Anthropic()

def next_state(current: WorkflowState) -> WorkflowState:
    transitions = {
        WorkflowState.COLLECT_GOAL:        WorkflowState.COLLECT_CONSTRAINTS,
        WorkflowState.COLLECT_CONSTRAINTS:  WorkflowState.COLLECT_TECH_STACK,
        WorkflowState.COLLECT_TECH_STACK:   WorkflowState.GENERATE_PLAN,
        WorkflowState.GENERATE_PLAN:        WorkflowState.REVIEW_AND_CONFIRM,
        WorkflowState.REVIEW_AND_CONFIRM:   WorkflowState.DONE,
    }
    return transitions.get(current, WorkflowState.DONE)

def process_step(ctx: WorkflowContext, user_input: str) -> tuple[WorkflowContext, str]:
    if ctx.state == WorkflowState.COLLECT_GOAL:
        ctx.goal = user_input
        ctx.state = next_state(ctx.state)
        return ctx, STATE_PROMPTS[ctx.state]

    if ctx.state == WorkflowState.COLLECT_CONSTRAINTS:
        ctx.constraints = user_input
        ctx.state = next_state(ctx.state)
        return ctx, STATE_PROMPTS[ctx.state]

    if ctx.state == WorkflowState.COLLECT_TECH_STACK:
        ctx.tech_stack = user_input
        ctx.state = next_state(ctx.state)
        # Auto-generate plan
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            messages=[{"role": "user", "content": f"""Create a deployment plan:
Goal: {ctx.goal}
Constraints: {ctx.constraints}
Tech Stack: {ctx.tech_stack}

Provide a numbered step-by-step deployment plan."""}]
        )
        ctx.draft_plan = response.content[0].text
        ctx.state = next_state(ctx.state)  # Move to REVIEW
        return ctx, f"Here is your draft plan:\n\n{ctx.draft_plan}\n\n{STATE_PROMPTS[WorkflowState.REVIEW_AND_CONFIRM]}"

    if ctx.state == WorkflowState.REVIEW_AND_CONFIRM:
        if user_input.lower().strip() in ("yes", "y", "looks good", "approve", "ok"):
            ctx.confirmed = True
            ctx.state = WorkflowState.DONE
            return ctx, f"Plan finalized! Here is your approved deployment plan:\n\n{ctx.draft_plan}"
        else:
            # Revise plan based on feedback
            response = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=512,
                messages=[{"role": "user", "content": f"""Revise this deployment plan based on feedback:

Original plan:
{ctx.draft_plan}

Feedback: {user_input}

Provide the revised plan."""}]
            )
            ctx.draft_plan = response.content[0].text
            return ctx, f"Revised plan:\n\n{ctx.draft_plan}\n\n{STATE_PROMPTS[WorkflowState.REVIEW_AND_CONFIRM]}"

    return ctx, "Workflow complete."

# Simulate a user session
ctx = WorkflowContext()
print(f"[Step 1] {STATE_PROMPTS[WorkflowState.COLLECT_GOAL]}")

steps = [
    "Deploy our Python FastAPI service to AWS with zero downtime",
    "2 weeks timeline, 3 engineers, no weekend downtime allowed",
    "Python 3.11, FastAPI, PostgreSQL, AWS ECS, GitHub Actions",
    "yes",
]

for user_input in steps:
    print(f"\nUser: {user_input}")
    ctx, response = process_step(ctx, user_input)
    print(f"Agent: {response[:200]}")
    if ctx.state == WorkflowState.DONE:
        break

print(f"\nWorkflow complete. Confirmed: {ctx.confirmed}")

# Expected Token Savings: Collecting context before generation reduces revision rounds. One well-informed plan generation vs 3–5 blind attempts. Saves ~60% of total tokens on complex planning tasks.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 2: Question Queue with Progress Tracking

Maintain an ordered queue of questions. Extract answers from each user response and advance through the queue, skipping questions already answered.

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class WorkflowQuestion:
    question_id: str
    question: str
    answer: Optional[str] = None
    required: bool = True
    depends_on: Optional[str] = None  # question_id this depends on

@dataclass
class QuestionQueue:
    questions: list[WorkflowQuestion]
    current_index: int = 0
    collected: dict[str, str] = field(default_factory=dict)

    def next_unanswered(self) -> Optional[WorkflowQuestion]:
        for q in self.questions:
            if q.answer is not None:
                continue
            if q.depends_on and self.collected.get(q.depends_on) is None:
                continue
            return q
        return None

    def record_answer(self, question_id: str, answer: str):
        for q in self.questions:
            if q.question_id == question_id:
                q.answer = answer
                self.collected[question_id] = answer
                return

    def is_complete(self) -> bool:
        return all(q.answer is not None for q in self.questions if q.required)

    def summary(self) -> dict:
        return {q.question_id: q.answer for q in self.questions if q.answer}

client = anthropic.Anthropic()

def extract_answer(question: str, user_response: str) -> str:
    """Use LLM to extract a clean answer from potentially verbose user input."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"""Extract the direct answer to this question from the user's response.

Question: {question}
User response: {user_response}

Return just the answer, concise and direct. No preamble."""
        }]
    )
    return response.content[0].text.strip()

def generate_deliverable(answers: dict) -> str:
    context = "\n".join(f"- {k}: {v}" for k, v in answers.items())
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Based on these requirements, create a project proposal:

{context}

Write a concise 3-paragraph proposal."""
        }]
    )
    return response.content[0].text

def run_question_queue_workflow(user_inputs: list[str]) -> str:
    queue = QuestionQueue(questions=[
        WorkflowQuestion("project_name",    "What is the name of your project?"),
        WorkflowQuestion("problem_solved",  "What problem does it solve?"),
        WorkflowQuestion("target_users",    "Who are the target users?"),
        WorkflowQuestion("timeline",        "What is your target launch timeline?"),
        WorkflowQuestion("budget",          "What is the approximate budget?", required=False),
        WorkflowQuestion("team_size",       "How many people are on the team?"),
        WorkflowQuestion("tech_preferences","Any technology preferences or constraints?", required=False),
    ])

    input_idx = 0
    pending_question = queue.next_unanswered()

    while pending_question and input_idx < len(user_inputs):
        print(f"\n[Question] {pending_question.question}")
        user_response = user_inputs[input_idx]
        input_idx += 1
        print(f"User: {user_response}")

        answer = extract_answer(pending_question.question, user_response)
        queue.record_answer(pending_question.question_id, answer)
        print(f"[Extracted] {pending_question.question_id} = {answer}")

        pending_question = queue.next_unanswered()
        if pending_question:
            print(f"[Next] {pending_question.question}")

    if queue.is_complete():
        print(f"\n[Complete] Collected {len(queue.collected)} answers")
        return generate_deliverable(queue.summary())
    else:
        missing = [q.question_id for q in queue.questions if q.required and q.answer is None]
        return f"Incomplete workflow — still need: {missing}"

user_inputs = [
    "It's called QuickDeploy",
    "It solves the problem of slow manual deployments by automating CI/CD pipelines",
    "DevOps engineers and small dev teams",
    "We're targeting a Q3 2025 launch",
    "We have a team of 4 engineers",
]

result = run_question_queue_workflow(user_inputs)
print(f"\nFinal Deliverable:\n{result[:300]}")

# Expected Token Savings: Answer extraction with Haiku costs ~100 tokens per question. Final generation with full context costs ~300 tokens once. Total ~900 tokens vs multiple failed blind attempts at ~500 tokens each.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 3: Conversational Wizard with Dynamic Question Generation

Use the LLM to dynamically generate follow-up questions based on previous answers, adapting the workflow to the user's specific situation.

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class WizardState:
    topic: str
    answers: dict[str, str] = field(default_factory=dict)
    questions_asked: list[str] = field(default_factory=list)
    max_questions: int = 5
    ready_to_generate: bool = False

client = anthropic.Anthropic()

def generate_next_question(state: WizardState) -> str | None:
    """Ask the LLM what to ask next based on what we know so far."""
    if len(state.questions_asked) >= state.max_questions:
        return None

    context = json.dumps(state.answers, indent=2) if state.answers else "No answers yet"
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"""You are gathering information to help with: {state.topic}

Information collected so far:
{context}

Questions already asked: {state.questions_asked}

What is the single most important question to ask next?
If you have enough information to proceed, respond with exactly: READY

Otherwise, respond with just the question (no preamble)."""
        }]
    )
    answer = response.content[0].text.strip()
    if answer == "READY":
        return None
    return answer

def extract_key_value(question: str, answer: str, topic: str) -> tuple[str, str]:
    """Extract a clean key-value pair from a Q&A."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"For the topic '{topic}', this Q&A pair: Q: {question} A: {answer}\nExtract as JSON: {{\"key\": \"short_key\", \"value\": \"clean_answer\"}}"
        }]
    )
    text = response.content[0].text.strip()
    try:
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text.strip())
        return data["key"], data["value"]
    except Exception:
        return question[:30].lower().replace(" ", "_"), answer

def generate_output(state: WizardState) -> str:
    context = "\n".join(f"- {k}: {v}" for k, v in state.answers.items())
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Create a detailed {state.topic} based on these requirements:

{context}

Provide actionable, specific output."""
        }]
    )
    return response.content[0].text

def run_adaptive_wizard(topic: str, user_answers: list[str]) -> str:
    state = WizardState(topic=topic)
    answer_idx = 0

    while answer_idx < len(user_answers):
        question = generate_next_question(state)
        if question is None:
            state.ready_to_generate = True
            break

        state.questions_asked.append(question)
        print(f"\n[Wizard] {question}")

        if answer_idx >= len(user_answers):
            break

        user_answer = user_answers[answer_idx]
        answer_idx += 1
        print(f"User: {user_answer}")

        key, value = extract_key_value(question, user_answer, topic)
        state.answers[key] = value
        print(f"[Stored] {key} = {value}")

    print(f"\n[Generating] with {len(state.answers)} collected data points...")
    return generate_output(state)

result = run_adaptive_wizard(
    topic="API integration plan",
    user_answers=[
        "We need to integrate with Stripe for payments and SendGrid for emails",
        "Python FastAPI backend, PostgreSQL database",
        "About 10,000 requests per day initially",
        "We need PCI compliance for payment handling",
        "2 senior engineers, 6 weeks timeline",
    ]
)
print(f"\nOutput:\n{result[:300]}")

# Expected Token Savings: Adaptive questioning stops when sufficient context is collected — never asks unnecessary questions. Average 3–4 questions vs fixed 8-question wizards. Saves 4 Haiku calls and improves UX.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 4: Parallel Information Gathering with Merge Step

Collect independent pieces of information in parallel, then merge into a single context before generation.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class InfoRequest:
    field_id: str
    question: str
    answer: Optional[str] = None

@dataclass
class ParallelWorkflowResult:
    gathered: dict[str, str]
    missing: list[str]
    output: str

client = anthropic.AsyncAnthropic()

async def ask_for_info(field_id: str, question: str, user_input: str) -> tuple[str, str]:
    """Extract structured answer from user input for a specific field."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"Extract answer for '{field_id}' from: '{user_input}'\nQuestion was: {question}\nReturn only the answer value."
        }]
    )
    return field_id, response.content[0].text.strip()

async def gather_all_info_parallel(
    info_requests: list[InfoRequest],
    single_user_input: str
) -> dict[str, str]:
    """Try to extract all required fields from a single comprehensive user input."""
    tasks = [
        ask_for_info(req.field_id, req.question, single_user_input)
        for req in info_requests
    ]
    results = await asyncio.gather(*tasks)
    return dict(results)

async def multi_stage_parallel_workflow(
    goal: str,
    initial_input: str,
    follow_up_inputs: dict[str, str] = None
) -> ParallelWorkflowResult:
    REQUIRED_FIELDS = [
        InfoRequest("audience",   "Who is the target audience?"),
        InfoRequest("constraints","What are the key constraints?"),
        InfoRequest("success_metric", "How will success be measured?"),
        InfoRequest("timeline",   "What is the timeline?"),
        InfoRequest("resources",  "What resources are available?"),
    ]

    print("[Stage 1] Parallel extraction from initial input")
    gathered = await gather_all_info_parallel(REQUIRED_FIELDS, initial_input)
    print(f"[Extracted] {list(gathered.keys())}")

    # Identify thin or missing answers
    thin_fields = [
        fid for fid, answer in gathered.items()
        if len(answer.split()) < 3 or answer.lower() in ("none", "unknown", "n/a", "")
    ]

    # Fill in from follow-up inputs if available
    if thin_fields and follow_up_inputs:
        print(f"[Stage 2] Filling gaps: {thin_fields}")
        for field_id in thin_fields:
            if field_id in follow_up_inputs:
                gathered[field_id] = follow_up_inputs[field_id]

    missing = [f for f in thin_fields if f not in (follow_up_inputs or {})]

    # Generate output with available context
    context = "\n".join(f"- {k}: {v}" for k, v in gathered.items())
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Create a detailed plan for: {goal}

Requirements gathered:
{context}

{'NOTE: Some information was not provided: ' + str(missing) if missing else ''}

Provide a concrete, actionable plan."""
        }]
    )

    return ParallelWorkflowResult(
        gathered=gathered,
        missing=missing,
        output=response.content[0].text
    )

async def main():
    result = await multi_stage_parallel_workflow(
        goal="Launch a developer documentation portal",
        initial_input="""We need to build docs for our API. Our audience is external developers
        integrating with our payment SDK. We have 3 months and a team of 2 tech writers and 1 engineer.
        Success means 80% of developers can integrate without support tickets.""",
        follow_up_inputs={
            "constraints": "Must use existing GitHub repo, budget under $5k/month"
        }
    )

    print(f"\nGathered: {list(result.gathered.keys())}")
    print(f"Missing: {result.missing}")
    print(f"\nOutput:\n{result.output[:300]}")

asyncio.run(main())

# Expected Token Savings: Parallel field extraction uses 5 simultaneous Haiku calls (same wall time as 1). Total extraction cost: ~500 tokens. Avoids 5 sequential question rounds (~1000 tokens in conversation history overhead).
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 5: SQLite-Persisted Workflow with Resume Support

Persist workflow state to SQLite so interrupted workflows can resume from where they left off across sessions.

```python
import anthropic
import sqlite3
import json
import uuid
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class WorkflowSession:
    session_id: str
    workflow_type: str
    step: int
    collected: dict
    status: str  # "in_progress" | "complete" | "abandoned"
    created_at: float
    updated_at: float

client = anthropic.Anthropic()

def init_workflow_db(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_sessions (
            session_id TEXT PRIMARY KEY,
            workflow_type TEXT,
            step INTEGER DEFAULT 0,
            collected TEXT DEFAULT '{}',
            status TEXT DEFAULT 'in_progress',
            created_at REAL,
            updated_at REAL
        )
    """)
    conn.commit()
    return conn

def create_session(conn: sqlite3.Connection, workflow_type: str) -> str:
    session_id = str(uuid.uuid4())
    now = time.time()
    conn.execute(
        "INSERT INTO workflow_sessions VALUES (?,?,?,?,?,?,?)",
        (session_id, workflow_type, 0, '{}', 'in_progress', now, now)
    )
    conn.commit()
    return session_id

def load_session(conn: sqlite3.Connection, session_id: str) -> Optional[WorkflowSession]:
    row = conn.execute(
        "SELECT * FROM workflow_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if not row:
        return None
    return WorkflowSession(
        session_id=row[0], workflow_type=row[1], step=row[2],
        collected=json.loads(row[3]), status=row[4],
        created_at=row[5], updated_at=row[6]
    )

def save_session(conn: sqlite3.Connection, session: WorkflowSession):
    conn.execute("""
        UPDATE workflow_sessions
        SET step=?, collected=?, status=?, updated_at=?
        WHERE session_id=?
    """, (session.step, json.dumps(session.collected), session.status, time.time(), session.session_id))
    conn.commit()

WORKFLOW_STEPS = [
    ("project_name",    "What is the name of your project?"),
    ("objective",       "What is the main objective or problem being solved?"),
    ("stakeholders",    "Who are the key stakeholders?"),
    ("success_criteria","How will you measure success?"),
    ("timeline",        "What is your target completion date?"),
]

def run_workflow_step(session: WorkflowSession, user_input: str) -> tuple[WorkflowSession, str]:
    if session.step >= len(WORKFLOW_STEPS):
        session.status = "complete"
        return session, "Workflow already complete."

    step_key, step_question = WORKFLOW_STEPS[session.step]
    session.collected[step_key] = user_input
    session.step += 1

    if session.step >= len(WORKFLOW_STEPS):
        # Generate final output
        context = "\n".join(f"- {k}: {v}" for k, v in session.collected.items())
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Create a project charter based on:\n{context}"
            }]
        )
        session.status = "complete"
        return session, f"Project charter complete:\n\n{response.content[0].text}"

    next_question = WORKFLOW_STEPS[session.step][1]
    progress = f"[Step {session.step}/{len(WORKFLOW_STEPS)}] "
    return session, progress + next_question

def resume_or_start(
    conn: sqlite3.Connection,
    session_id: Optional[str],
    user_input: str
) -> tuple[str, str, WorkflowSession]:
    if session_id:
        session = load_session(conn, session_id)
        if session and session.status == "in_progress":
            print(f"[resume] Session {session_id[:8]} at step {session.step}")
        else:
            session = None

    if not session_id or not session:
        session_id = create_session(conn, "project_charter")
        session = load_session(conn, session_id)
        print(f"[new] Session {session_id[:8]}")

    session, response = run_workflow_step(session, user_input)
    save_session(conn, session)
    return session_id, response, session

conn = init_workflow_db()

# Simulate a session that gets interrupted and resumes
print("=== Starting workflow ===")
first_question = WORKFLOW_STEPS[0][1]
print(f"[Step 0/{len(WORKFLOW_STEPS)}] {first_question}")

session_id = None
inputs = [
    "Project Phoenix",
    "Reduce customer churn by 20% through better onboarding",
    "Product team, Customer Success, Engineering",
    # Simulate interruption after 3 inputs
]

for user_input in inputs:
    print(f"\nUser: {user_input}")
    session_id, response, session = resume_or_start(conn, session_id, user_input)
    print(f"Agent: {response[:120]}")

print(f"\n=== Simulating resume after interruption (step={session.step}) ===")
resume_inputs = [
    "NPS > 40 and churn rate below 5% within 6 months",
    "Q4 2025",
]
for user_input in resume_inputs:
    print(f"\nUser: {user_input}")
    session_id, response, session = resume_or_start(conn, session_id, user_input)
    print(f"Agent: {response[:200]}")

print(f"\nFinal status: {session.status}")

# Expected Token Savings: Persisted workflow prevents starting over after interruption. Resumed sessions skip already-answered questions. For a 5-step workflow, resuming from step 3 saves 60% of collection tokens.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3 (stdlib). Change DB path for persistence across restarts.
```

---

## Option 6: Parallel Sub-Agent Workflow Orchestration

Break the workflow into specialized sub-agents that each handle one phase, run independently, and pass their output to the next phase.

```python
import anthropic
import asyncio
from dataclasses import dataclass

@dataclass
class Phase:
    name: str
    system_prompt: str
    input_key: str   # key from context dict to use as input
    output_key: str  # key to write output to

@dataclass
class PipelineContext:
    user_request: str
    phase_outputs: dict

client = anthropic.AsyncAnthropic()

PIPELINE_PHASES = [
    Phase(
        name="Requirements Analyst",
        system_prompt="Extract structured requirements from user input. Be specific and numbered.",
        input_key="user_request",
        output_key="requirements"
    ),
    Phase(
        name="Solution Designer",
        system_prompt="Design a solution based on the requirements. Focus on architecture and components.",
        input_key="requirements",
        output_key="solution_design"
    ),
    Phase(
        name="Risk Assessor",
        system_prompt="Identify top 3 risks in this solution and mitigation strategies.",
        input_key="solution_design",
        output_key="risk_assessment"
    ),
    Phase(
        name="Implementation Planner",
        system_prompt="Create a week-by-week implementation plan based on the solution and risk assessment.",
        input_key="solution_design",
        output_key="implementation_plan"
    ),
]

async def run_phase(phase: Phase, ctx: PipelineContext) -> tuple[str, str]:
    input_content = ctx.phase_outputs.get(phase.input_key) or ctx.user_request

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are a {phase.name}. {phase.system_prompt}",
        messages=[{"role": "user", "content": input_content[:500]}]
    )
    output = response.content[0].text
    print(f"[{phase.name}] → {phase.output_key} ({len(output)} chars)")
    return phase.output_key, output

async def run_sequential_phases(phases: list[Phase], ctx: PipelineContext) -> PipelineContext:
    """Run phases sequentially where each depends on the previous."""
    ctx.phase_outputs["user_request"] = ctx.user_request
    for phase in phases:
        key, value = await run_phase(phase, ctx)
        ctx.phase_outputs[key] = value
    return ctx

async def run_parallel_phases(
    phases: list[Phase],
    ctx: PipelineContext,
    required_input_keys: list[str]
) -> PipelineContext:
    """Run independent phases in parallel when inputs are already available."""
    ready = [p for p in phases if p.input_key in ctx.phase_outputs]
    results = await asyncio.gather(*[run_phase(p, ctx) for p in ready])
    for key, value in results:
        ctx.phase_outputs[key] = value
    return ctx

async def run_full_workflow(user_request: str) -> dict:
    ctx = PipelineContext(user_request=user_request, phase_outputs={})
    ctx.phase_outputs["user_request"] = user_request

    print("[Phase 1] Requirements Analysis")
    key, value = await run_phase(PIPELINE_PHASES[0], ctx)
    ctx.phase_outputs[key] = value

    print("[Phase 2] Solution Design (depends on requirements)")
    key, value = await run_phase(PIPELINE_PHASES[1], ctx)
    ctx.phase_outputs[key] = value

    print("[Phase 3+4] Risk Assessment + Implementation Plan (parallel, both need solution_design)")
    results = await asyncio.gather(
        run_phase(PIPELINE_PHASES[2], ctx),
        run_phase(PIPELINE_PHASES[3], ctx),
    )
    for key, value in results:
        ctx.phase_outputs[key] = value

    # Final synthesis
    summary_input = "\n\n".join([
        f"Requirements:\n{ctx.phase_outputs.get('requirements', '')}",
        f"Solution:\n{ctx.phase_outputs.get('solution_design', '')}",
        f"Risks:\n{ctx.phase_outputs.get('risk_assessment', '')}",
        f"Plan:\n{ctx.phase_outputs.get('implementation_plan', '')}",
    ])
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Create an executive summary from:\n{summary_input[:1500]}"}]
    )
    ctx.phase_outputs["executive_summary"] = response.content[0].text
    return ctx.phase_outputs

outputs = asyncio.run(run_full_workflow(
    "Build a real-time collaborative document editing system for our 500-person company"
))
print(f"\nPhases completed: {list(outputs.keys())}")
print(f"\nExecutive Summary:\n{outputs.get('executive_summary', '')[:300]}")

# Expected Token Savings: Parallel phases 3+4 run simultaneously — saves 1 full round-trip latency. Sequential phases ensure correct dependency order. Total: 5 LLM calls vs 5 sequential with 4x the latency.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Comparison

| Option | Workflow Type | Resumable | Parallel | Adaptive | Best For |
|--------|--------------|-----------|----------|----------|----------|
| 1: State Machine | Fixed steps | No | No | No | Predictable linear workflows |
| 2: Question Queue | Ordered questions | No | No | Skip answered | Form-style data collection |
| 3: Adaptive Wizard | LLM-generated questions | No | No | Yes | Open-ended requirement gathering |
| 4: Parallel Extraction | All-at-once then gaps | No | Yes | No | Comprehensive user input extraction |
| 5: SQLite-Persisted | Fixed steps | Yes | No | No | Long-running workflows, mobile/web UX |
| 6: Sub-Agent Pipeline | Phase-based | No | Phase 3+4 | No | Complex multi-discipline planning |
