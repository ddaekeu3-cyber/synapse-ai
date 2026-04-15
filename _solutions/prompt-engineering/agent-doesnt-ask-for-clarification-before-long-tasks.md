---
layout: solution
title: "Agent Doesn't Ask for Clarification Before Long Tasks"
category: prompt-engineering
description: "Agent immediately begins a complex, multi-step, irreversible task based on an ambiguous instruction, spending thousands of tokens and minutes of work before discovering that it misunderstood the core requirement."
tags: [prompt-engineering, clarification, long-tasks, disambiguation, efficiency, ux]
---

## Symptom

A user asks: "Refactor the authentication system." The agent immediately generates 800 lines of refactored code across 12 files, runs tests, and outputs a complete PR description. The user replies: "I meant just clean up the variable names, not rewrite the whole thing." The agent wasted 15,000 tokens and 3 minutes. Alternatively, the user says "Write a report on our customers" and the agent produces a 2,000-word marketing analysis when the user wanted a 5-row CSV export.

## Root Cause

Agents are trained to be helpful and action-oriented, which creates a bias toward attempting tasks rather than asking questions. For short tasks, this is usually fine — misunderstandings are cheap to correct. For long, expensive, or irreversible tasks, immediate execution without clarification is a systematic anti-pattern. The agent needs to distinguish between tasks that are safe to attempt immediately and tasks where ambiguity poses a significant risk of wasted work.

## Fix

### Option 1 — Task complexity classifier: ask before long tasks, act on short ones

```python
import json
import anthropic

client = anthropic.Anthropic()

COMPLEXITY_SYSTEM = """Classify whether this task requires clarification before starting.

Classify as CLARIFY if:
- The task would take more than 2 minutes or 500 tokens to complete
- The task modifies files, databases, or external systems (irreversible)
- The user's intent could reasonably be interpreted in 2+ different ways
- The scope is ambiguous (e.g., "refactor X" — how much? which parts?)

Classify as PROCEED if:
- The task is short and reversible
- The intent is unambiguous
- A wrong guess is cheap to correct

Return JSON: {
  "classification": "CLARIFY" | "PROCEED",
  "ambiguities": ["list of unclear points if CLARIFY"],
  "clarifying_questions": ["1-3 specific questions to ask if CLARIFY"]
}"""

TASK_SYSTEM = "You are a helpful software assistant. Complete the user's task precisely."

def handle_task(user_request: str) -> str:
    # Step 1: assess whether clarification is needed
    r_assess = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=COMPLEXITY_SYSTEM,
        messages=[{"role": "user", "content": user_request}],
    )
    raw = r_assess.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        assessment = json.loads(raw)
    except json.JSONDecodeError:
        assessment = {"classification": "PROCEED"}

    classification = assessment.get("classification", "PROCEED")
    print(f"  [assess] classification={classification}")

    if classification == "CLARIFY":
        questions = assessment.get("clarifying_questions", ["Could you clarify the scope?"])
        lines = ["Before I start, I need a few clarifications:\n"]
        for i, q in enumerate(questions[:3], 1):
            lines.append(f"{i}. {q}")
        return "\n".join(lines)

    # Step 2: proceed with execution
    r_task = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=TASK_SYSTEM,
        messages=[{"role": "user", "content": user_request}],
    )
    return r_task.content[0].text

requests = [
    "What does HTTP stand for?",                                    # short → PROCEED
    "Refactor the authentication system.",                          # ambiguous → CLARIFY
    "Write a report on our Q4 customers.",                          # ambiguous → CLARIFY
    "Convert this string to uppercase: 'hello world'",             # clear → PROCEED
    "Migrate our database to the new schema.",                      # risky → CLARIFY
]
for req in requests:
    print(f"Request: {req}")
    response = handle_task(req)
    print(f"Response: {response[:200]}\n")
```

**Expected Token Savings:** Clarification before a 15,000-token misaligned task costs ~200 tokens; preventing one misaligned long task saves 14,800 tokens — a 98% reduction for that request.
**Environment:** Agents handling mixed-complexity tasks; complexity classification is the first line of defence against wasted work on ambiguous instructions.

---

### Option 2 — Explicit scope confirmation for destructive or long operations

```python
import anthropic

client = anthropic.Anthropic()

# Operations that always require explicit scope confirmation
HIGH_RISK_KEYWORDS = {
    "refactor", "migrate", "rewrite", "delete", "remove", "drop", "reset",
    "overwrite", "replace", "update all", "rebuild", "restructure",
}

SCOPE_CONFIRM_SYSTEM = """You are a careful assistant. For any task that is:
- Destructive (deletes, overwrites, or modifies many things)
- Long-running (would take more than a few minutes)
- Broad in scope (e.g., "all", "entire", "whole system")

You MUST:
1. Restate your understanding of the task in one sentence.
2. List the specific actions you plan to take (bullet points).
3. Ask the user to confirm or correct before proceeding.

Format:
**My understanding:** [one sentence]
**Planned actions:**
- [action 1]
- [action 2]
...
**Please confirm or clarify before I proceed.**

For simple, safe, or clearly-scoped tasks, just do them without asking."""

def handle_request(request: str) -> str:
    request_lower = request.lower()
    needs_confirm = any(kw in request_lower for kw in HIGH_RISK_KEYWORDS)

    if needs_confirm:
        print(f"  [scope] high-risk keywords detected — requiring confirmation")
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=SCOPE_CONFIRM_SYSTEM,
            messages=[{"role": "user", "content": request}],
        )
    else:
        print(f"  [scope] safe request — proceeding directly")
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": request}],
        )
    return r.content[0].text

requests = [
    "Delete all inactive users from the database.",
    "What is Python?",
    "Rewrite the login module to use OAuth2.",
    "Add 1 + 1.",
    "Remove all deprecated API endpoints and update the docs.",
]
for req in requests:
    print(f"Request: {req}")
    print(f"Response: {handle_request(req)[:250]}\n")
```

**Expected Token Savings:** Keyword-based detection adds zero LLM calls — it's a free client-side check; by catching high-risk operations before they start, it prevents multi-thousand-token wasted executions caused by scope misunderstandings.
**Environment:** Agents with filesystem, database, or code modification tools; keyword detection is the simplest and cheapest guard against runaway destructive operations.

---

### Option 3 — Clarification budget: ask at most one round of questions

```python
import json
import anthropic

client = anthropic.Anthropic()

CLARIFY_SYSTEM = """You are a precise assistant. Before starting complex tasks, identify the single most important unknown.

Analyze the request. If there is ONE critical ambiguity that would change your entire approach, ask about it.
If the task is clear enough to start, or if you can make a reasonable default assumption, just do it.

Rules:
- Ask at MOST ONE question per request.
- If you ask a question, also state your DEFAULT ASSUMPTION so the user can just say "yes" to accept it.
- Never ask obvious questions (e.g., "What programming language?" when Python is clearly implied).

Format when asking:
QUESTION: [specific question]
MY DEFAULT: [what you'll assume if user says yes/go ahead]

Format when proceeding:
PROCEEDING: [one-sentence plan summary]
[then do the task]"""

def ask_or_proceed(request: str) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=CLARIFY_SYSTEM,
        messages=[{"role": "user", "content": request}],
    )
    return r.content[0].text

def full_interaction(request: str, user_followup: str | None = None) -> str:
    """Simulate a two-turn clarification interaction."""
    response1 = ask_or_proceed(request)
    print(f"Agent: {response1[:250]}")

    if "QUESTION:" in response1 and user_followup:
        # Second turn: answer the clarifying question and get the result
        print(f"\nUser: {user_followup}")
        r2 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[
                {"role": "user",      "content": request},
                {"role": "assistant", "content": response1},
                {"role": "user",      "content": user_followup},
            ],
        )
        return r2.content[0].text
    return response1

requests_and_followups = [
    ("Write a report on sales.",           "Yes, use the Q4 data and keep it under 500 words."),
    ("Refactor the user class.",           "Just rename the variables to snake_case."),
    ("What is 2 + 2?",                     None),   # no question needed
    ("Set up monitoring for the service.", "Yes, use Prometheus and alert on error rate > 1%."),
]
for request, followup in requests_and_followups:
    print(f"\nUser: {request}")
    result = full_interaction(request, followup)
    if followup and "QUESTION:" in result:
        print(f"Final: {result[:200]}")
    print("-" * 50)
```

**Expected Token Savings:** One-question budget prevents clarification loops (3-5 back-and-forth turns at 300 tokens each) by forcing the agent to identify the single most blocking ambiguity; stating the default assumption lets users confirm with one word instead of writing a paragraph.
**Environment:** Customer-facing agents where multiple clarifying questions feel interrogative; the one-question budget balances thoroughness with user experience.

---

### Option 4 — Pre-task plan display with confirmation checkpoint

```python
import anthropic

client = anthropic.Anthropic()

PLAN_SYSTEM = """You are a careful assistant. For any task that involves multiple steps, modifications, or significant work:

1. Write a SHORT execution plan (3-7 bullet points).
2. Estimate the scope: "This will change approximately N files / take N steps."
3. End with: "Reply 'go' to proceed, or tell me what to change."

For trivial one-step tasks, skip the plan and just do them immediately."""

EXECUTE_SYSTEM = "Complete the task as planned. Be precise and thorough."

def plan_then_execute(request: str, user_approval: str = "go") -> dict:
    # Step 1: generate plan
    r_plan = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=PLAN_SYSTEM,
        messages=[{"role": "user", "content": request}],
    )
    plan = r_plan.content[0].text
    print(f"Plan:\n{plan[:300]}")

    # Simulate user approval (in production: wait for real user input)
    if "go" not in user_approval.lower() and len(user_approval) > 3:
        # User wants changes — incorporate feedback
        print(f"\nUser revision: {user_approval}")
        request = f"{request}\n\nRevision: {user_approval}"

    # Step 2: execute
    print(f"\nExecuting plan...")
    r_exec = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=EXECUTE_SYSTEM,
        messages=[
            {"role": "user",      "content": request},
            {"role": "assistant", "content": plan},
            {"role": "user",      "content": user_approval},
        ],
    )
    return {
        "plan":   plan,
        "result": r_exec.content[0].text,
        "tokens": r_plan.usage.output_tokens + r_exec.usage.output_tokens,
    }

scenarios = [
    ("Add error handling to the payment module.", "go"),
    ("Write unit tests for the auth service.",    "Only cover the login and logout functions."),
    ("What is REST?",                             "go"),
]
for request, approval in scenarios:
    print(f"\nRequest: {request}")
    result = plan_then_execute(request, approval)
    print(f"\nFinal result: {result['result'][:250]}")
    print(f"Total output tokens: {result['tokens']}")
    print("=" * 60)
```

**Expected Token Savings:** Plan display costs ~150 tokens and catches misalignments before execution — if the plan reveals a 10-file change when the user expected a 1-file change, the correction prevents 9× the expected execution cost; the "go" approval adds one token from the user.
**Environment:** Agents making code changes or multi-file operations; plan-then-confirm is the standard pattern in agentic coding tools (Cursor, GitHub Copilot Workspace) for exactly this reason.

---

### Option 5 — Ambiguity score: act only if confidence exceeds threshold

```python
import json
import anthropic

client = anthropic.Anthropic()

AMBIGUITY_SYSTEM = """Analyze this task request and score its ambiguity.

Ambiguity score: 0 = perfectly clear, 10 = completely unclear
Consider:
- Are the inputs fully specified?
- Is the desired output format clear?
- Is the scope bounded?
- Could reasonable people interpret this differently?

Return JSON:
{
  "ambiguity_score": 0-10,
  "unclear_dimensions": ["list of unclear aspects"],
  "minimum_viable_interpretation": "the narrowest safe interpretation",
  "recommended_question": "the one most useful clarifying question, or null if score < 3"
}"""

EXECUTE_SYSTEM = "Complete the task according to the interpretation provided."

AMBIGUITY_THRESHOLD = 5   # scores >= this require clarification

def smart_clarify(request: str) -> str:
    # Score ambiguity
    r_score = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=AMBIGUITY_SYSTEM,
        messages=[{"role": "user", "content": request}],
    )
    raw = r_score.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        analysis = {"ambiguity_score": 0, "minimum_viable_interpretation": request}

    score    = analysis.get("ambiguity_score", 0)
    min_interp = analysis.get("minimum_viable_interpretation", request)
    question = analysis.get("recommended_question")
    print(f"  [ambiguity] score={score}/10 unclear={analysis.get('unclear_dimensions', [])[:2]}")

    if score >= AMBIGUITY_THRESHOLD and question:
        return f"{question}\n\n(If you'd like me to proceed with the conservative interpretation — {min_interp} — just say 'proceed'.)"

    # Low ambiguity: proceed with the minimum viable interpretation
    r_exec = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=EXECUTE_SYSTEM,
        messages=[{"role": "user", "content": f"Task: {request}\n\nInterpretation: {min_interp}"}],
    )
    return r_exec.content[0].text

requests = [
    "What is 42 in binary?",                          # score ≈ 0 → proceed
    "Clean up the codebase.",                          # score ≈ 8 → clarify
    "Add logging to the authentication module.",       # score ≈ 4 → borderline
    "Delete the test database and recreate it.",       # score ≈ 2 → proceed (clear)
    "Improve performance.",                            # score ≈ 9 → clarify
]
for req in requests:
    print(f"\nRequest: {req}")
    print(f"Response: {smart_clarify(req)[:200]}")
```

**Expected Token Savings:** Ambiguity scoring adds ~100 tokens per request but prevents costly misaligned executions on high-ambiguity tasks; the minimum viable interpretation prevents the agent from doing nothing when clarification isn't strictly required.
**Environment:** General-purpose agents handling developer or analyst requests; ambiguity scoring is most useful when tasks vary widely in specificity and the cost of getting it wrong varies correspondingly.

---

### Option 6 — Multi-turn clarification guard: limit clarification rounds to 2

```python
import anthropic

client = anthropic.Anthropic()

MAX_CLARIFICATION_ROUNDS = 2

AGENT_SYSTEM = """You are a helpful assistant. Follow these rules about clarification:

Rule 1: If you genuinely cannot proceed without more information, ask ONE specific question.
Rule 2: NEVER ask more than 2 clarifying questions total before attempting the task.
Rule 3: If you have asked 2 questions already, make your best guess and proceed.
Rule 4: State your assumptions explicitly when proceeding with incomplete information.
Rule 5: For short, reversible tasks — just do them. Wrong answers are cheap to fix."""

def run_clarification_loop(initial_request: str, simulated_answers: list[str]) -> str:
    """
    Simulate a multi-turn clarification interaction.
    simulated_answers: what the user would reply to each clarification request.
    """
    messages = [{"role": "user", "content": initial_request}]
    clarifications_used = 0

    for turn in range(MAX_CLARIFICATION_ROUNDS + 2):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=AGENT_SYSTEM,
            messages=messages,
        )
        response = r.content[0].text
        print(f"Agent (turn {turn+1}): {response[:200]}")

        # Check if agent is asking a question
        is_question = "?" in response and any(
            word in response.lower() for word in ["would you", "could you", "which", "what", "how many", "should i"]
        )

        if not is_question:
            print(f"\n[Completed after {turn+1} turn(s), {clarifications_used} clarification(s)]")
            return response

        clarifications_used += 1
        if clarifications_used > MAX_CLARIFICATION_ROUNDS:
            print(f"\n[Clarification limit reached — agent must proceed]")
            # Force the agent to proceed
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": "Please proceed with your best judgment and state your assumptions."})
            r_final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=AGENT_SYSTEM,
                messages=messages,
            )
            return r_final.content[0].text

        # Provide the simulated user answer
        if turn < len(simulated_answers):
            user_reply = simulated_answers[turn]
        else:
            user_reply = "Please use your best judgment."
        print(f"User: {user_reply}\n")
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user",      "content": user_reply})

    return "max turns reached"

result = run_clarification_loop(
    initial_request="Write a data pipeline.",
    simulated_answers=["It should process CSV files.", "Output to PostgreSQL."],
)
print(f"\nFinal result: {result[:300]}")
```

**Expected Token Savings:** Capping clarification at 2 rounds prevents the degenerate case where the agent asks 6-8 questions before starting, consuming more tokens in clarification than the task itself; the "proceed with assumptions" fallback ensures every request eventually produces output.
**Environment:** All agents; the clarification round limit is a defensive guard that prevents clarification-loop anti-patterns where agents refuse to act without perfect information.

---

## Comparison

| Option | Detection Method | Extra Calls | Max Clarification Turns | Best For |
|---|---|---|---|---|
| 1. Complexity classifier | LLM | +1 (classifier) | 1 | Mixed complexity agents |
| 2. Keyword detection | String match | 0 | 1 | Quick guard for destructive operations |
| 3. One-question budget | LLM | 0 (inline) | 1 | User-facing agents, UX-sensitive |
| 4. Plan-then-confirm | LLM | +1 (plan) | 1 | Code modification agents |
| 5. Ambiguity score | LLM | +1 (score) | 1 | Variable-scope developer agents |
| 6. Multi-turn guard | Runtime counter | 0 | 2 (capped) | All agents — defensive backstop |
