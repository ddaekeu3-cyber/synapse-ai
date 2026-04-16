---
title: "Agent Doesn't Implement Plan Revision on Repeated Failure"
description: "How to detect when an agent is repeatedly failing at the same step and trigger an explicit plan revision rather than retrying the same failing approach indefinitely."
categories: [loop-stuck]
difficulty: intermediate
---

When an agent's current plan isn't working, retrying the same steps with the same approach just wastes tokens and time. Plan revision on repeated failure detects the pattern—same step, same error—and forces a fundamentally different strategy rather than re-executing the same failing path.

## Solution 1: Failure Counter with Plan Reset

Count consecutive failures at the same step. Above a threshold, discard the current plan and re-plan from scratch.

```python
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"
FAILURE_THRESHOLD = 3


@dataclass
class StepResult:
    step: str
    success: bool
    error: str | None = None


@dataclass
class PlanState:
    goal: str
    steps: list[str] = field(default_factory=list)
    current_step_index: int = 0
    failure_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    revision_count: int = 0
    completed_steps: list[str] = field(default_factory=list)

    @property
    def current_step(self) -> str | None:
        if self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None


async def generate_plan(goal: str, failed_approaches: list[str] = []) -> list[str]:
    avoid_section = ""
    if failed_approaches:
        avoid_section = (
            "\n\nPrevious approaches that failed (do NOT use these):\n"
            + "\n".join(f"- {a}" for a in failed_approaches)
        )

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Create a step-by-step plan (3-5 steps) to accomplish this goal:\n{goal}"
                    f"{avoid_section}\n\n"
                    f"Return ONLY the steps, one per line, no numbering."
                ),
            }
        ],
    )
    steps = [line.strip() for line in resp.content[0].text.strip().splitlines() if line.strip()]
    return steps[:5]


async def attempt_step(step: str, attempt_num: int) -> StepResult:
    """Simulate step execution — occasionally fails."""
    import random
    # Simulate that some steps are fundamentally broken
    if "search web" in step.lower() and attempt_num < 3:
        return StepResult(step=step, success=False, error="Network timeout")
    return StepResult(step=step, success=True)


async def run_with_plan_revision(goal: str, max_revisions: int = 3) -> str:
    failed_approaches: list[str] = []
    state = PlanState(goal=goal)
    state.steps = await generate_plan(goal)

    print(f"Initial plan: {state.steps}")

    for iteration in range(30):
        if state.current_step is None:
            return f"Goal achieved after {state.revision_count} plan revisions."

        step = state.current_step
        result = await attempt_step(step, state.failure_counts[step] + 1)

        if result.success:
            state.completed_steps.append(step)
            state.current_step_index += 1
            state.failure_counts[step] = 0
            print(f"  [OK] {step}")
        else:
            state.failure_counts[step] += 1
            print(f"  [FAIL #{state.failure_counts[step]}] {step}: {result.error}")

            if state.failure_counts[step] >= FAILURE_THRESHOLD:
                if state.revision_count >= max_revisions:
                    return f"[GAVE UP] Exceeded {max_revisions} plan revisions."

                # Record the failed approach and revise
                failed_approaches.append(step)
                state.revision_count += 1
                print(f"  [REVISE] Plan revision #{state.revision_count}")

                remaining_goal = f"{goal} (completed so far: {', '.join(state.completed_steps) or 'nothing'})"
                state.steps = await generate_plan(remaining_goal, failed_approaches)
                state.current_step_index = 0
                state.failure_counts.clear()
                print(f"  New plan: {state.steps}")

    return "[MAX ITERATIONS] Stopped."


async def main():
    goal = "Research and summarize the latest developments in quantum computing"
    result = await run_with_plan_revision(goal, max_revisions=2)
    print(f"\nFinal: {result}")


asyncio.run(main())
```

## Solution 2: Error Pattern Classifier with Strategy Switch

Classify the error type and switch to a pre-defined alternative strategy for each error class.

```python
import asyncio
from dataclasses import dataclass
from enum import Enum
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


class ErrorClass(Enum):
    NETWORK = "network"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    NOT_FOUND = "not_found"
    FORMAT = "format"
    LOGIC = "logic"
    UNKNOWN = "unknown"


# Alternative strategies for each error class
ERROR_STRATEGIES: dict[ErrorClass, list[str]] = {
    ErrorClass.NETWORK: [
        "retry with exponential backoff",
        "use cached data if available",
        "try alternative data source",
    ],
    ErrorClass.AUTH: [
        "refresh credentials",
        "use read-only fallback",
        "request user re-authentication",
    ],
    ErrorClass.RATE_LIMIT: [
        "wait and retry after delay",
        "batch requests to reduce frequency",
        "use cached results",
    ],
    ErrorClass.NOT_FOUND: [
        "search for alternative resource",
        "use approximate match",
        "report as unavailable and skip",
    ],
    ErrorClass.FORMAT: [
        "try different output format",
        "use more explicit format instructions",
        "apply post-processing repair",
    ],
    ErrorClass.LOGIC: [
        "decompose into smaller sub-tasks",
        "request human clarification",
        "use simpler alternative approach",
    ],
    ErrorClass.UNKNOWN: [
        "retry once",
        "skip step and continue",
        "escalate to human operator",
    ],
}


async def classify_error(error_message: str) -> ErrorClass:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=20,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Classify this error: {error_message!r}\n"
                    f"Categories: network, auth, rate_limit, not_found, format, logic, unknown\n"
                    f"Reply with only the category name."
                ),
            }
        ],
    )
    raw = resp.content[0].text.strip().lower()
    try:
        return ErrorClass(raw)
    except ValueError:
        return ErrorClass.UNKNOWN


@dataclass
class StrategyTracker:
    step: str
    error_class: ErrorClass
    strategy_index: int = 0

    @property
    def current_strategy(self) -> str | None:
        strategies = ERROR_STRATEGIES.get(self.error_class, [])
        if self.strategy_index < len(strategies):
            return strategies[self.strategy_index]
        return None

    def advance(self) -> bool:
        self.strategy_index += 1
        return self.current_strategy is not None


async def execute_with_strategy(step: str, strategy: str | None) -> tuple[bool, str | None]:
    """Simulate execution. In production, re-prompt the model with the new strategy."""
    await asyncio.sleep(0.01)
    # Simulate that different strategies have different success rates
    import random
    if strategy and "alternative" in strategy:
        return True, None
    if strategy and "skip" in strategy:
        return True, None  # Treat skip as success
    return False, f"Step failed: {step}"


async def run_with_strategy_switching(steps: list[str], goal: str) -> str:
    for step in steps:
        strategy_tracker = None
        attempt = 0

        while attempt < 5:
            strategy = strategy_tracker.current_strategy if strategy_tracker else None
            success, error = await execute_with_strategy(step, strategy)

            if success:
                print(f"  [OK] {step}" + (f" (via: {strategy})" if strategy else ""))
                break

            # First failure — classify and set up strategy tracker
            if strategy_tracker is None:
                error_class = await classify_error(error or "")
                strategy_tracker = StrategyTracker(step=step, error_class=error_class)
                print(f"  [FAIL] {step} → error class: {error_class.value}")
            else:
                has_more = strategy_tracker.advance()
                if not has_more:
                    print(f"  [EXHAUSTED] All strategies tried for: {step}")
                    break

            attempt += 1

    return "Completed with strategy switching."


async def main():
    steps = [
        "Fetch data from external API",
        "Parse and validate the response",
        "Store results to database",
    ]
    result = await run_with_strategy_switching(steps, "Collect and store market data")
    print(f"\n{result}")


asyncio.run(main())
```

## Solution 3: Hypothesis-Based Re-Planning

When stuck, ask the model to hypothesize why the current plan is failing and generate a plan that addresses the root cause.

```python
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"


@dataclass
class FailureHistory:
    step: str
    errors: list[str] = field(default_factory=list)
    attempts: int = 0


async def diagnose_failures(goal: str, history: list[FailureHistory]) -> str:
    history_text = "\n".join(
        f"Step: {h.step}\n  Attempts: {h.attempts}\n  Errors: {h.errors}"
        for h in history
    )
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\n\n"
                    f"These steps have been failing:\n{history_text}\n\n"
                    f"In 2-3 sentences, diagnose why this plan is failing and "
                    f"what fundamentally different approach should be tried."
                ),
            }
        ],
    )
    return resp.content[0].text


async def generate_revised_plan(goal: str, diagnosis: str, completed: list[str]) -> list[str]:
    completed_text = ", ".join(completed) if completed else "nothing yet"
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\n"
                    f"Already completed: {completed_text}\n\n"
                    f"Diagnosis of why previous plan failed:\n{diagnosis}\n\n"
                    f"Create a NEW 3-5 step plan that addresses this diagnosis. "
                    f"Return only the steps, one per line."
                ),
            }
        ],
    )
    return [l.strip() for l in resp.content[0].text.strip().splitlines() if l.strip()][:5]


async def hypothesis_replanning_agent(goal: str, max_replans: int = 2) -> str:
    completed_steps: list[str] = []
    current_plan: list[str] = []
    failure_history: list[FailureHistory] = []
    replans = 0

    # Generate initial plan
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": f"Plan 3-5 steps to: {goal}\nOne step per line."}],
    )
    current_plan = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()][:5]
    print(f"Initial plan: {current_plan}")

    step_idx = 0
    while step_idx < len(current_plan):
        step = current_plan[step_idx]
        fh = next((h for h in failure_history if h.step == step), None)
        if fh is None:
            fh = FailureHistory(step=step)
            failure_history.append(fh)

        # Simulate step execution (fails on first 2 attempts)
        import random
        success = fh.attempts >= 2 or random.random() > 0.4
        fh.attempts += 1

        if success:
            completed_steps.append(step)
            step_idx += 1
            print(f"  [OK] {step}")
        else:
            fh.errors.append(f"Simulated failure #{fh.attempts}")
            print(f"  [FAIL #{fh.attempts}] {step}")

            if fh.attempts >= 3 and replans < max_replans:
                print(f"  [DIAGNOSE] Generating hypothesis...")
                diagnosis = await diagnose_failures(goal, [h for h in failure_history if h.attempts >= 2])
                print(f"  [DIAGNOSIS] {diagnosis[:200]}")

                current_plan = await generate_revised_plan(goal, diagnosis, completed_steps)
                replans += 1
                step_idx = 0
                failure_history = []
                print(f"  [REVISED] New plan: {current_plan}")

            elif fh.attempts >= 5:
                return f"[STUCK] Could not complete: {step}"

    return f"[DONE] Goal achieved after {replans} re-plans."


async def main():
    result = await hypothesis_replanning_agent(
        "Build a Python web scraper to collect product prices",
        max_replans=2,
    )
    print(f"\n{result}")


asyncio.run(main())
```

## Solution 4: Constraint Relaxation Re-Planner

When a plan fails due to constraint violations, systematically relax constraints to find a viable path.

```python
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"


@dataclass
class Constraint:
    name: str
    description: str
    priority: int  # 1=must-have, 2=should-have, 3=nice-to-have
    relaxed: bool = False


async def plan_with_constraints(goal: str, constraints: list[Constraint]) -> list[str]:
    active = [c for c in constraints if not c.relaxed]
    constraint_text = "\n".join(
        f"[{'MUST' if c.priority == 1 else 'SHOULD' if c.priority == 2 else 'NICE'}] {c.description}"
        for c in active
    )
    relaxed = [c for c in constraints if c.relaxed]
    relaxed_text = ("\nRelaxed constraints (no longer required): " +
                    ", ".join(c.name for c in relaxed)) if relaxed else ""

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\n"
                    f"Constraints:\n{constraint_text}{relaxed_text}\n\n"
                    f"Create a 3-5 step plan. One step per line."
                ),
            }
        ],
    )
    return [l.strip() for l in resp.content[0].text.splitlines() if l.strip()][:5]


async def attempt_plan(steps: list[str], constraints: list[Constraint]) -> tuple[bool, str]:
    """Simulate plan execution. Fails if too many must-have constraints are active."""
    active_must = sum(1 for c in constraints if c.priority == 1 and not c.relaxed)
    import random
    if active_must > 2:
        return False, "Too many hard constraints — no viable execution path found"
    if random.random() < 0.3:
        return False, "Resource unavailable"
    return True, ""


async def constraint_relaxation_agent(goal: str) -> str:
    constraints = [
        Constraint("no_external_apis", "Must not call any external APIs", priority=2),
        Constraint("single_thread", "Must run in single-threaded mode", priority=2),
        Constraint("no_disk_writes", "Must not write to disk", priority=3),
        Constraint("deterministic", "Must produce deterministic output", priority=1),
    ]

    for relaxation_round in range(len(constraints) + 1):
        print(f"\n[Round {relaxation_round}] Generating plan with current constraints")
        active = [c for c in constraints if not c.relaxed]
        print(f"  Active: {[c.name for c in active]}")

        steps = await plan_with_constraints(goal, constraints)
        success, error = await attempt_plan(steps, constraints)

        if success:
            relaxed = [c.name for c in constraints if c.relaxed]
            return (f"[SUCCESS] Goal achieved.\n"
                    f"  Steps: {steps}\n"
                    f"  Relaxed constraints: {relaxed or 'none'}")

        print(f"  [FAIL] {error}")

        # Relax the lowest-priority un-relaxed constraint
        candidates = [c for c in constraints if not c.relaxed and c.priority >= 2]
        candidates.sort(key=lambda c: -c.priority)  # Relax nice-to-haves first

        if not candidates:
            return f"[FAILED] Cannot relax any more constraints."

        to_relax = candidates[0]
        to_relax.relaxed = True
        print(f"  [RELAX] Relaxing constraint: {to_relax.name}")

    return "[FAILED] All strategies exhausted."


async def main():
    result = await constraint_relaxation_agent(
        "Process and analyze a large dataset of customer transactions"
    )
    print(f"\n{result}")


asyncio.run(main())
```

## Solution 5: Collaborative Re-Planning with Critic Agent

When the primary agent is stuck, spawn a critic agent that reviews the failure and proposes a revised plan.

```python
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
EXECUTOR_MODEL = "claude-haiku-4-5-20251001"
CRITIC_MODEL = "claude-sonnet-4-6"


@dataclass
class ExecutionTrace:
    goal: str
    attempts: list[dict] = field(default_factory=list)

    def record(self, step: str, outcome: str, error: str | None = None):
        self.attempts.append({"step": step, "outcome": outcome, "error": error})

    def to_text(self) -> str:
        lines = [f"Goal: {self.goal}"]
        for a in self.attempts:
            status = "SUCCESS" if a["outcome"] == "success" else f"FAILED: {a['error']}"
            lines.append(f"  Step: {a['step']} → {status}")
        return "\n".join(lines)


async def critic_review_and_replan(trace: ExecutionTrace) -> list[str]:
    resp = await client.messages.create(
        model=CRITIC_MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"The following execution trace shows a stuck agent:\n\n"
                    f"{trace.to_text()}\n\n"
                    f"As a critic, identify the core problem and propose a revised 3-5 step plan "
                    f"that avoids the failures above. Return ONLY the new steps, one per line."
                ),
            }
        ],
    )
    return [l.strip() for l in resp.content[0].text.splitlines() if l.strip()][:5]


async def collaborative_replan_agent(goal: str, max_critic_calls: int = 2) -> str:
    trace = ExecutionTrace(goal=goal)
    critic_calls = 0

    # Initial plan
    init_resp = await client.messages.create(
        model=EXECUTOR_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": f"Plan 3 steps to: {goal}. One per line."}],
    )
    steps = [l.strip() for l in init_resp.content[0].text.splitlines() if l.strip()][:3]

    consecutive_failures = 0
    step_idx = 0

    while step_idx < len(steps):
        step = steps[step_idx]
        import random
        success = consecutive_failures >= 3 or random.random() > 0.5

        if success:
            trace.record(step, "success")
            consecutive_failures = 0
            step_idx += 1
            print(f"  [OK] {step}")
        else:
            error = f"Error on attempt {consecutive_failures + 1}"
            trace.record(step, "failure", error)
            consecutive_failures += 1
            print(f"  [FAIL] {step}: {error}")

            if consecutive_failures >= 3 and critic_calls < max_critic_calls:
                print(f"  [CRITIC] Requesting plan revision...")
                steps = await critic_review_and_replan(trace)
                critic_calls += 1
                consecutive_failures = 0
                step_idx = 0
                print(f"  [CRITIC REVISED] New plan: {steps}")
            elif consecutive_failures >= 6:
                return f"[EXHAUSTED] Critic couldn't fix the plan after {critic_calls} reviews."

    return f"[DONE] Goal achieved with {critic_calls} critic-driven revisions."


async def main():
    result = await collaborative_replan_agent(
        "Generate and validate a comprehensive test suite for an API",
        max_critic_calls=2,
    )
    print(f"\n{result}")


asyncio.run(main())
```

## Solution 6: Probabilistic Plan Sampling

Instead of one deterministic plan, maintain a portfolio of N candidate plans and retire failing ones.

```python
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"
PORTFOLIO_SIZE = 3


@dataclass
class CandidatePlan:
    plan_id: str
    steps: list[str]
    success_count: int = 0
    failure_count: int = 0
    active: bool = True

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.5

    @property
    def is_viable(self) -> bool:
        if self.failure_count < 2:
            return True  # Not enough data
        return self.success_rate >= 0.4


async def generate_diverse_plans(goal: str, n: int = PORTFOLIO_SIZE) -> list[CandidatePlan]:
    """Generate N diverse candidate plans using temperature variation."""
    plans = []
    for i in range(n):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Create a unique approach #{i+1} (different from other approaches) "
                        f"to accomplish: {goal}\nList 3-4 steps, one per line."
                    ),
                }
            ],
        )
        steps = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()][:4]
        plans.append(CandidatePlan(plan_id=f"plan_{i+1}", steps=steps))
    return plans


async def execute_best_plan_step(plans: list[CandidatePlan]) -> tuple[CandidatePlan | None, str, bool]:
    """Pick the most viable plan and execute its next step."""
    viable = [p for p in plans if p.active and p.is_viable and p.steps]
    if not viable:
        return None, "", False

    # Pick plan with highest success rate (or first if tied)
    best = max(viable, key=lambda p: p.success_rate)
    step = best.steps[0]

    import random
    # Simulate execution
    success = random.random() > 0.35

    if success:
        best.success_count += 1
        best.steps = best.steps[1:]  # Consume step
    else:
        best.failure_count += 1
        if not best.is_viable:
            best.active = False
            print(f"  [RETIRE] {best.plan_id} retired (success_rate={best.success_rate:.0%})")

    return best, step, success


async def portfolio_planning_agent(goal: str) -> str:
    plans = await generate_diverse_plans(goal, n=PORTFOLIO_SIZE)
    print(f"Generated {len(plans)} candidate plans:")
    for p in plans:
        print(f"  {p.plan_id}: {p.steps}")

    for iteration in range(20):
        active_plans = [p for p in plans if p.active and p.steps]
        if not active_plans:
            break

        plan, step, success = await execute_best_plan_step(plans)
        if plan is None:
            break

        status = "OK" if success else "FAIL"
        print(f"  [{status}] {plan.plan_id}: {step}")

        # Check if any plan completed
        if plan and not plan.steps and plan.success_count > 0:
            return f"[DONE] {plan.plan_id} completed all steps."

    active = [p for p in plans if p.active]
    if active:
        return f"[PARTIAL] {len(active)} plans still viable but max iterations reached."
    return "[FAILED] All candidate plans exhausted."


async def main():
    result = await portfolio_planning_agent("Migrate a legacy database to a new schema")
    print(f"\n{result}")


asyncio.run(main())
```

## Comparison

| Solution | Re-plan trigger | LLM calls for revision | Learns from failure | Best for |
|---|---|---|---|---|
| **Failure counter + reset** | Count threshold | 1 (new plan) | No | Simple retry loops |
| **Error pattern + strategy** | Error classification | 1 (classify) | No | Known error types |
| **Hypothesis re-planning** | Count threshold | 2 (diagnose + plan) | Yes | Complex multi-step tasks |
| **Constraint relaxation** | Execution failure | 1 (replan) | No | Constraint-heavy environments |
| **Critic agent** | Count threshold | 1 (critic review) | Yes | High-stakes autonomous agents |
| **Portfolio sampling** | Success rate | N (parallel plans) | Yes | Uncertain/exploratory tasks |

Start with **failure counter + reset** (Solution 1) — minimal complexity, effective for most loops. Upgrade to **hypothesis re-planning** (Solution 3) when you need the agent to actually learn from what went wrong. Use **portfolio sampling** (Solution 6) for exploratory tasks where the right approach is unknown upfront.
