---
layout: solution
title: "Agent Doesn't Implement Token Budget Forecasting"
category: token-cost
description: "How to estimate total token consumption before starting long multi-step tasks, warn when projected costs exceed budgets, and abort or downscale before expensive runaway executions."
tags: [token-cost, forecasting, budget, estimation, multi-step, cost-control]
---

# Agent Doesn't Implement Token Budget Forecasting

Sending a 50-step research task without estimating total cost first can silently consume hundreds of dollars. Token budget forecasting estimates total consumption before the first API call, warns when projections exceed limits, and lets you abort, downscale, or route to cheaper models before committing to expensive executions.

## Option 1: Static Task Complexity Estimator

Estimate total tokens from task description using a rule-based model before any API calls.

```python
import anthropic
from dataclasses import dataclass
from typing import Optional
import re


@dataclass
class TokenForecast:
    estimated_steps: int
    tokens_per_step: int
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: float
    model: str
    confidence: str         # "high", "medium", "low"

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def __str__(self) -> str:
        return (
            f"Forecast [{self.confidence} confidence]: "
            f"{self.estimated_steps} steps × {self.tokens_per_step} tokens/step = "
            f"{self.total_tokens:,} tokens | ~${self.estimated_cost_usd:.4f} ({self.model})"
        )


COST_PER_TOKEN = {
    "claude-haiku-4-5-20251001": {"input": 0.00000025, "output": 0.00000125},
    "claude-sonnet-4-6":  {"input": 0.000003,   "output": 0.000015},
    "claude-opus-4-6":    {"input": 0.000015,   "output": 0.000075},
}


def estimate_steps(task_description: str) -> tuple[int, str]:
    """Estimate number of agent steps from task description."""
    lower = task_description.lower()

    # High step-count signals
    if any(kw in lower for kw in ["comprehensive", "exhaustive", "complete guide", "all", "every"]):
        return 15, "low"  # Vague scope → hard to estimate

    # Step count from explicit numbers
    num_match = re.search(r"\b(\d+)\s+(steps?|items?|examples?|sections?|parts?)\b", lower)
    if num_match:
        n = int(num_match.group(1))
        return min(n * 2, 30), "high"  # Each item may take 2 turns

    # Task type heuristics
    if any(kw in lower for kw in ["research", "analyze", "investigate", "study"]):
        return 8, "medium"
    if any(kw in lower for kw in ["write", "draft", "create", "generate"]):
        return 4, "medium"
    if any(kw in lower for kw in ["summarize", "explain", "describe"]):
        return 2, "high"
    if any(kw in lower for kw in ["what", "when", "who", "where"]):
        return 1, "high"

    return 5, "low"


def estimate_tokens_per_step(task_description: str, model: str) -> int:
    """Estimate average tokens consumed per step."""
    lower = task_description.lower()

    # Code tasks generate more output
    if any(kw in lower for kw in ["code", "implement", "function", "script"]):
        return 800
    # Analysis tasks have large context
    if any(kw in lower for kw in ["analyze", "research", "report"]):
        return 1200
    # Simple Q&A
    if any(kw in lower for kw in ["what", "who", "when", "list"]):
        return 200

    return 500  # Default


def forecast_task_cost(
    task_description: str,
    model: str = "claude-sonnet-4-6",
    system_prompt_tokens: int = 200,
) -> TokenForecast:
    steps, confidence = estimate_steps(task_description)
    tokens_per_step = estimate_tokens_per_step(task_description, model)

    input_tokens = steps * (system_prompt_tokens + tokens_per_step // 2)
    output_tokens = steps * (tokens_per_step // 2)

    rates = COST_PER_TOKEN.get(model, COST_PER_TOKEN["claude-sonnet-4-6"])
    cost = input_tokens * rates["input"] + output_tokens * rates["output"]

    return TokenForecast(
        estimated_steps=steps,
        tokens_per_step=tokens_per_step,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        estimated_cost_usd=cost,
        model=model,
        confidence=confidence,
    )


def run_with_budget_gate(
    task: str,
    model: str = "claude-sonnet-4-6",
    budget_usd: float = 0.10,
    auto_abort: bool = False,
) -> Optional[str]:
    forecast = forecast_task_cost(task, model)
    print(forecast)

    if forecast.estimated_cost_usd > budget_usd:
        msg = (
            f"Forecast ${forecast.estimated_cost_usd:.4f} exceeds budget ${budget_usd:.4f}. "
            f"Reduce scope or increase budget."
        )
        if auto_abort:
            print(f"[ABORT] {msg}")
            return None
        print(f"[WARNING] {msg}")

    # Proceed with actual execution
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=forecast.tokens_per_step,
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text


if __name__ == "__main__":
    tasks = [
        ("What is 2+2?", "claude-haiku-4-5-20251001", 0.01),
        ("Write a comprehensive 20-section guide to building production ML systems.", "claude-opus-4-6", 0.50),
        ("Summarize the key points of transformer architecture.", "claude-sonnet-4-6", 0.05),
    ]

    for task, model, budget in tasks:
        print(f"\nTask: {task[:60]}")
        result = run_with_budget_gate(task, model, budget, auto_abort=True)
        if result:
            print(f"Result: {result[:80]}...")

# Expected Token Savings: 100% savings on aborted over-budget tasks; 30-50% savings from early downscaling
# Environment: Batch processing systems, autonomous agents, any workflow where task scope is user-defined
```

## Option 2: Haiku Pre-Planner for Accurate Step Count Estimation

Use a cheap Haiku call to decompose the task and count actual steps before committing to the full model.

```python
import anthropic
import json
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class TaskPlan:
    steps: list[str]
    estimated_output_tokens_per_step: list[int]
    total_estimated_tokens: int
    estimated_cost_usd: float
    model: str

    def summary(self) -> str:
        lines = [f"Plan ({len(self.steps)} steps, ~{self.total_estimated_tokens:,} tokens, ~${self.estimated_cost_usd:.4f}):"]
        for i, (step, tokens) in enumerate(zip(self.steps, self.estimated_output_tokens_per_step)):
            lines.append(f"  {i+1}. {step[:60]} (~{tokens} tokens)")
        return "\n".join(lines)


PLANNER_SYSTEM = """You are a task planner. Break down the user's task into discrete steps.
For each step, estimate how many output tokens it will require.

Respond with JSON only:
{
  "steps": ["step 1 description", "step 2 description", ...],
  "tokens_per_step": [estimated_output_tokens_per_step, ...]
}

Be concise in step descriptions. Token estimates: simple=50-100, medium=200-500, complex=500-1500."""


def plan_task_with_haiku(task: str) -> Optional[TaskPlan]:
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": task}],
    )

    text = response.content[0].text
    try:
        json_match = re.search(r"\{[\s\S]+\}", text)
        data = json.loads(json_match.group()) if json_match else {}
        steps = data.get("steps", [])
        tokens_per_step = data.get("tokens_per_step", [500] * len(steps))

        # Pad/trim tokens list to match steps
        while len(tokens_per_step) < len(steps):
            tokens_per_step.append(500)
        tokens_per_step = tokens_per_step[:len(steps)]

        total_tokens = sum(tokens_per_step)
        cost = total_tokens * 0.000015  # Sonnet output rate

        return TaskPlan(
            steps=steps,
            estimated_output_tokens_per_step=tokens_per_step,
            total_estimated_tokens=total_tokens,
            estimated_cost_usd=cost,
            model="claude-sonnet-4-6",
        )
    except Exception as e:
        print(f"[PLANNER] Parse error: {e}")
        return None


def execute_with_plan_gate(
    task: str,
    budget_usd: float = 0.20,
    model: str = "claude-sonnet-4-6",
) -> Optional[str]:
    print(f"[PLANNER] Analyzing task...")
    plan = plan_task_with_haiku(task)

    if not plan:
        print("[PLANNER] Could not generate plan. Proceeding with defaults.")
    else:
        print(plan.summary())

        if plan.estimated_cost_usd > budget_usd:
            print(f"\n[BUDGET GATE] Estimated ${plan.estimated_cost_usd:.4f} > budget ${budget_usd:.4f}")
            print(f"[BUDGET GATE] Suggest reducing to {int(budget_usd / plan.estimated_cost_usd * len(plan.steps))} steps")
            return f"[Aborted: projected cost ${plan.estimated_cost_usd:.4f} exceeds ${budget_usd:.4f} budget]"

    client = anthropic.Anthropic()
    max_tokens = plan.total_estimated_tokens if plan else 1000

    response = client.messages.create(
        model=model,
        max_tokens=min(max_tokens, 4096),
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text


if __name__ == "__main__":
    test_cases = [
        ("Explain what REST means.", 0.05),
        ("Write a complete tutorial on building a FastAPI backend with authentication, database integration, and deployment.", 0.10),
        ("List 3 Python testing frameworks.", 0.02),
    ]

    for task, budget in test_cases:
        print(f"\n{'='*60}\nTask: {task[:70]}")
        result = execute_with_plan_gate(task, budget_usd=budget)
        if result and not result.startswith("[Aborted"):
            print(f"Result: {result[:100]}...")

# Expected Token Savings: 40-70% from accurate step counting; eliminates over-allocation and aborts runaway tasks
# Environment: Autonomous agents, complex multi-step workflows with user-defined scope
```

## Option 3: Running Cost Accumulator with Dynamic Abort

Track actual token usage during multi-turn execution. Abort mid-run when accumulated cost approaches the budget ceiling.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunningCostTracker:
    budget_usd: float
    model: str
    warn_at_pct: float = 0.75      # Warn at 75% of budget
    abort_at_pct: float = 0.95     # Abort at 95% of budget
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turn_count: int = 0

    RATES = {
        "claude-haiku-4-5-20251001": {"input": 0.00000025, "output": 0.00000125},
        "claude-sonnet-4-6":  {"input": 0.000003,   "output": 0.000015},
        "claude-opus-4-6":    {"input": 0.000015,   "output": 0.000075},
    }

    @property
    def current_cost(self) -> float:
        rates = self.RATES.get(self.model, self.RATES["claude-sonnet-4-6"])
        return (
            self.total_input_tokens * rates["input"] +
            self.total_output_tokens * rates["output"]
        )

    @property
    def remaining_budget(self) -> float:
        return max(0.0, self.budget_usd - self.current_cost)

    @property
    def budget_pct_used(self) -> float:
        return self.current_cost / self.budget_usd if self.budget_usd > 0 else 0.0

    def record_turn(self, input_tokens: int, output_tokens: int):
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.turn_count += 1

    def check_budget(self) -> tuple[str, str]:
        """Returns ('ok'|'warn'|'abort', message)."""
        pct = self.budget_pct_used
        if pct >= self.abort_at_pct:
            return "abort", f"Budget {pct:.0%} used (${self.current_cost:.4f}/${self.budget_usd:.4f})"
        if pct >= self.warn_at_pct:
            return "warn", f"Budget {pct:.0%} used (${self.remaining_budget:.4f} remaining)"
        return "ok", ""

    def max_output_tokens_remaining(self) -> int:
        """How many output tokens can we afford?"""
        rates = self.RATES.get(self.model, self.RATES["claude-sonnet-4-6"])
        affordable = self.remaining_budget / rates["output"]
        return max(50, min(int(affordable), 2048))


def multi_turn_with_budget_tracking(
    goal: str,
    model: str = "claude-sonnet-4-6",
    budget_usd: float = 0.05,
    max_turns: int = 10,
) -> str:
    client = anthropic.Anthropic()
    tracker = RunningCostTracker(budget_usd=budget_usd, model=model)

    messages = [{"role": "user", "content": goal}]
    final_output = ""

    for turn in range(max_turns):
        status, msg = tracker.check_budget()

        if status == "abort":
            print(f"[BUDGET ABORT] Turn {turn+1}: {msg}")
            # Inject final summary request with remaining budget
            messages.append({"role": "user", "content": (
                "Budget limit reached. Please provide the best summary of progress so far in 2 sentences."
            )})
            response = client.messages.create(
                model=model,
                max_tokens=100,
                messages=messages,
            )
            return response.content[0].text

        if status == "warn":
            print(f"[BUDGET WARN] Turn {turn+1}: {msg}")

        max_out = tracker.max_output_tokens_remaining()

        response = client.messages.create(
            model=model,
            max_tokens=max_out,
            messages=messages,
        )

        tracker.record_turn(
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        output = response.content[0].text
        final_output = output
        print(
            f"[Turn {turn+1}] {output[:50]}... "
            f"| ${tracker.current_cost:.4f}/{budget_usd:.4f} ({tracker.budget_pct_used:.0%})"
        )

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn":
            break

        messages.append({"role": "user", "content": "Continue."})

    print(f"\n[FINAL] Total: {tracker.total_input_tokens + tracker.total_output_tokens} tokens | ${tracker.current_cost:.4f}")
    return final_output


if __name__ == "__main__":
    result = multi_turn_with_budget_tracking(
        goal="Write a detailed explanation of how neural networks learn, covering forward pass, loss function, backpropagation, and weight updates.",
        model="claude-sonnet-4-6",
        budget_usd=0.005,  # tight budget to trigger abort
        max_turns=6,
    )
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: Dynamic abort saves 40-80% on over-scope tasks; gradual warning prevents sudden cutoffs
# Environment: Multi-turn autonomous agents, interactive sessions with per-request spending limits
```

## Option 4: Batch Task Pre-Screening — Estimate and Sort by Cost

Before processing a batch of tasks, estimate costs for all of them, sort cheapest-first, and abort when cumulative budget is hit.

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BatchTaskEstimate:
    task_id: str
    prompt: str
    estimated_tokens: int
    estimated_cost_usd: float
    priority: int = 0           # Higher = more important


def estimate_single_task_tokens(prompt: str) -> int:
    """Rule-based token estimator."""
    word_count = len(prompt.split())

    # Base: input tokens ≈ prompt words * 1.3
    input_tokens = int(word_count * 1.3)

    # Output estimation by task type
    if re.search(r"\b(write|implement|create|generate|build)\b", prompt, re.I):
        output_tokens = 600
    elif re.search(r"\b(explain|describe|analyze|discuss)\b", prompt, re.I):
        output_tokens = 300
    elif re.search(r"\b(summarize|list|enumerate)\b", prompt, re.I):
        output_tokens = 200
    else:
        output_tokens = 150

    return input_tokens + output_tokens


def pre_screen_batch(
    tasks: list[dict],         # Each dict has: task_id, prompt, priority
    total_budget_usd: float,
    model: str = "claude-sonnet-4-6",
    sort_strategy: str = "priority",  # "cheapest", "priority", "priority_density"
) -> tuple[list[BatchTaskEstimate], list[BatchTaskEstimate]]:
    """Returns (approved_tasks, rejected_tasks)."""
    rates = {
        "claude-haiku-4-5-20251001": 0.00000075,   # blended rate
        "claude-sonnet-4-6":  0.000009,
        "claude-opus-4-6":    0.000045,
    }
    rate = rates.get(model, 0.000009)

    estimates = []
    for task in tasks:
        tokens = estimate_single_task_tokens(task["prompt"])
        cost = tokens * rate
        estimates.append(BatchTaskEstimate(
            task_id=task["task_id"],
            prompt=task["prompt"],
            estimated_tokens=tokens,
            estimated_cost_usd=cost,
            priority=task.get("priority", 0),
        ))

    # Sort strategy
    if sort_strategy == "cheapest":
        estimates.sort(key=lambda e: e.estimated_cost_usd)
    elif sort_strategy == "priority":
        estimates.sort(key=lambda e: -e.priority)
    elif sort_strategy == "priority_density":
        # Maximize priority per dollar
        estimates.sort(key=lambda e: -(e.priority / max(e.estimated_cost_usd, 0.00001)))

    # Greedy budget allocation
    approved = []
    rejected = []
    cumulative = 0.0

    for estimate in estimates:
        if cumulative + estimate.estimated_cost_usd <= total_budget_usd:
            approved.append(estimate)
            cumulative += estimate.estimated_cost_usd
        else:
            rejected.append(estimate)

    print(f"[BATCH SCREEN] Budget: ${total_budget_usd:.4f}")
    print(f"  Approved: {len(approved)} tasks (~${cumulative:.4f})")
    print(f"  Rejected: {len(rejected)} tasks (over budget)")
    return approved, rejected


def execute_approved_batch(
    approved: list[BatchTaskEstimate],
    model: str = "claude-sonnet-4-6",
) -> dict[str, str]:
    client = anthropic.Anthropic()
    results = {}

    for i, task in enumerate(approved):
        print(f"[BATCH {i+1}/{len(approved)}] {task.task_id}: {task.prompt[:40]}")
        response = client.messages.create(
            model=model,
            max_tokens=task.estimated_tokens // 2,
            messages=[{"role": "user", "content": task.prompt}],
        )
        results[task.task_id] = response.content[0].text
        actual_cost = (response.usage.input_tokens + response.usage.output_tokens) * 0.000009
        print(f"  Done: ${actual_cost:.5f} actual vs ${task.estimated_cost_usd:.5f} estimated")

    return results


if __name__ == "__main__":
    tasks = [
        {"task_id": "T1", "prompt": "What is 3+3?", "priority": 1},
        {"task_id": "T2", "prompt": "Write a complete OAuth2 implementation in Python.", "priority": 5},
        {"task_id": "T3", "prompt": "Explain TCP handshake in 2 sentences.", "priority": 3},
        {"task_id": "T4", "prompt": "Summarize the CAP theorem.", "priority": 4},
        {"task_id": "T5", "prompt": "Create a comprehensive 30-chapter book on distributed systems.", "priority": 2},
        {"task_id": "T6", "prompt": "What does API stand for?", "priority": 3},
    ]

    approved, rejected = pre_screen_batch(tasks, total_budget_usd=0.05, sort_strategy="priority_density")
    print(f"\nApproved IDs: {[t.task_id for t in approved]}")
    print(f"Rejected IDs: {[t.task_id for t in rejected]}")

    results = execute_approved_batch(approved)
    for tid, result in results.items():
        print(f"\n{tid}: {result[:80]}...")

# Expected Token Savings: 30-70% by rejecting low-priority/high-cost tasks before any API calls
# Environment: Batch processing pipelines, nightly agent jobs, cost-capped automated workflows
```

## Option 5: Token Budget Projection with Confidence Intervals

Forecast token usage with low/median/high bounds so operators can set risk-appropriate budgets.

```python
import anthropic
import re
import statistics
from dataclasses import dataclass
from typing import Optional


@dataclass
class TokenProjection:
    p10_tokens: int      # Optimistic (10th percentile)
    p50_tokens: int      # Median expected
    p90_tokens: int      # Pessimistic (90th percentile)
    model: str

    def cost(self, percentile: int = 50) -> float:
        rates = {
            "claude-haiku-4-5-20251001": 0.00000075,
            "claude-sonnet-4-6":  0.000009,
            "claude-opus-4-6":    0.000045,
        }
        rate = rates.get(self.model, 0.000009)
        tokens = {10: self.p10_tokens, 50: self.p50_tokens, 90: self.p90_tokens}[percentile]
        return tokens * rate

    def __str__(self) -> str:
        return (
            f"Token projection [{self.model.split('-')[1]}]: "
            f"P10={self.p10_tokens:,} (${self.cost(10):.4f}) | "
            f"P50={self.p50_tokens:,} (${self.cost(50):.4f}) | "
            f"P90={self.p90_tokens:,} (${self.cost(90):.4f})"
        )


# Historical calibration data: task type → (p10, p50, p90) output tokens
CALIBRATION_TABLE = {
    "factual":        (30,  80,   200),
    "explanation":   (150, 350,   700),
    "code_small":    (200, 500,  1000),
    "code_large":    (500, 1200, 2500),
    "analysis":      (300, 700,  1500),
    "creative":      (200, 600,  1800),
    "summarization": (100, 250,   500),
    "unknown":       (100, 400,  1200),
}


def classify_task_type(prompt: str) -> str:
    lower = prompt.lower()
    if re.search(r"\b(what|who|when|where|is|are|define)\b.{0,50}\?$", lower):
        return "factual"
    if re.search(r"\b(write|implement|build|create).*(function|class|api|server)", lower):
        return "code_large" if len(prompt.split()) > 30 else "code_small"
    if re.search(r"\b(analyze|evaluate|assess|review|critique)\b", lower):
        return "analysis"
    if re.search(r"\b(explain|describe|how does|why does)\b", lower):
        return "explanation"
    if re.search(r"\b(summarize|summary|brief|tldr)\b", lower):
        return "summarization"
    if re.search(r"\b(write|compose|create|draft).*(story|poem|essay|article)\b", lower):
        return "creative"
    return "unknown"


def project_tokens(prompt: str, model: str = "claude-sonnet-4-6") -> TokenProjection:
    task_type = classify_task_type(prompt)
    p10, p50, p90 = CALIBRATION_TABLE[task_type]

    # Scale by prompt length (more context → more output)
    word_count = len(prompt.split())
    if word_count > 100:
        scale = min(2.0, word_count / 50)
        p10, p50, p90 = int(p10 * scale), int(p50 * scale), int(p90 * scale)

    # Add input tokens estimate
    input_est = int(word_count * 1.3) + 200  # +200 for system prompt

    return TokenProjection(
        p10_tokens=input_est + p10,
        p50_tokens=input_est + p50,
        p90_tokens=input_est + p90,
        model=model,
    )


def guarded_execute(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    budget_usd: float = 0.10,
    risk_tolerance: str = "p50",  # p10=optimistic, p50=median, p90=conservative
) -> Optional[str]:
    projection = project_tokens(prompt, model)
    print(projection)

    pct_map = {"p10": 10, "p50": 50, "p90": 90}
    projected_cost = projection.cost(pct_map.get(risk_tolerance, 50))
    print(f"Risk tolerance: {risk_tolerance} → projected cost ${projected_cost:.4f} vs budget ${budget_usd:.4f}")

    if projected_cost > budget_usd:
        print(f"[BLOCKED] {risk_tolerance} projection ${projected_cost:.4f} exceeds budget ${budget_usd:.4f}")
        return None

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=projection.p90_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    actual = response.usage.input_tokens + response.usage.output_tokens
    print(f"Actual: {actual:,} tokens (P50 was {projection.p50_tokens:,})")
    return response.content[0].text


if __name__ == "__main__":
    test_cases = [
        ("What is the speed of light?", 0.01, "p50"),
        ("Implement a complete Redis clone in Python.", 0.02, "p90"),
        ("Explain how garbage collection works.", 0.05, "p50"),
    ]

    for prompt, budget, risk in test_cases:
        print(f"\n{'='*60}\nTask: {prompt[:60]}")
        result = guarded_execute(prompt, budget_usd=budget, risk_tolerance=risk)
        print(f"Result: {'BLOCKED' if result is None else result[:80] + '...'}")

# Expected Token Savings: Conservative p90 budgeting prevents surprises; p10 mode maximizes throughput
# Environment: SRE-monitored batch jobs, cost-sensitive pipelines where budget variance matters
```

## Option 6: Model Downgrade Cascade on Forecast Breach

When the forecast for a target model exceeds the budget, automatically try cheaper models until one fits.

```python
import anthropic
from dataclasses import dataclass
from typing import Optional


@dataclass
class ModelOption:
    model_id: str
    cost_per_token: float
    quality_score: float   # 0.0–1.0, relative quality


MODEL_CASCADE = [
    ModelOption("claude-opus-4-6",           0.000045, 1.0),
    ModelOption("claude-sonnet-4-6",         0.000009, 0.85),
    ModelOption("claude-haiku-4-5-20251001", 0.00000075, 0.65),
]


def forecast_cost(prompt: str, model: ModelOption, max_tokens: int = 500) -> float:
    """Estimate cost for a single request."""
    input_tokens = len(prompt.split()) * 1.3 + 200
    output_tokens = max_tokens * 0.7  # Assume 70% utilization
    return (input_tokens + output_tokens) * model.cost_per_token


def cascade_route(
    prompt: str,
    budget_usd: float,
    max_tokens: int = 500,
    min_quality: float = 0.0,
) -> tuple[Optional[str], Optional[str]]:
    """Try models from most capable to cheapest until one fits the budget.
    Returns (response_text, model_used) or (None, None) if all exceed budget."""
    client = anthropic.Anthropic()

    for option in MODEL_CASCADE:
        if option.quality_score < min_quality:
            print(f"[CASCADE] {option.model_id.split('-')[1]}: skip (quality {option.quality_score:.2f} < min {min_quality})")
            continue

        estimated_cost = forecast_cost(prompt, option, max_tokens)
        print(f"[CASCADE] {option.model_id.split('-')[1]}: ~${estimated_cost:.5f} vs budget ${budget_usd:.5f}")

        if estimated_cost <= budget_usd:
            print(f"[CASCADE] Selected {option.model_id.split('-')[1]}")
            response = client.messages.create(
                model=option.model_id,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            actual_cost = (response.usage.input_tokens + response.usage.output_tokens) * option.cost_per_token
            print(f"[CASCADE] Actual cost: ${actual_cost:.5f}")
            return response.content[0].text, option.model_id

    print(f"[CASCADE] No model fits within ${budget_usd:.5f} budget")
    return None, None


if __name__ == "__main__":
    test_cases = [
        ("What is Python?", 0.001, 0.0),           # Haiku fits
        ("Explain neural networks in detail.", 0.01, 0.6),   # Sonnet fits, Haiku min quality too low
        ("Write a production ML pipeline.", 0.10, 0.8),       # Sonnet/Opus
        ("What is 2+2?", 0.000001, 0.0),            # Nothing fits this budget
    ]

    for prompt, budget, min_q in test_cases:
        print(f"\nPrompt: {prompt[:50]} | Budget: ${budget:.6f} | Min quality: {min_q}")
        result, model = cascade_route(prompt, budget_usd=budget, min_quality=min_q)
        if result:
            print(f"Response ({model.split('-')[1]}): {result[:80]}...")
        else:
            print("No suitable model found within budget")

# Expected Token Savings: 65-85% by routing cost-constrained requests to cheapest qualifying model automatically
# Environment: APIs with per-user budgets, tiered service plans, cost-sensitive production agents
```

## Comparison

| Option | Estimation Method | LLM Call | Accuracy | Best For |
|--------|-----------------|----------|----------|----------|
| 1 Static Rule Estimator | Keyword heuristics | None | 60-70% | Fast pre-screening, low overhead |
| 2 Haiku Pre-Planner | LLM task decomposition | 1 Haiku call | 80-88% | Unknown-scope tasks needing accurate step counts |
| 3 Running Accumulator | Live token tracking | None (during run) | 100% actual | Multi-turn agents with dynamic scope |
| 4 Batch Pre-Screening | Per-task estimation + sort | None | 65-75% | Batch jobs with priority-ordered work |
| 5 Confidence Intervals | Calibration table P10/P50/P90 | None | Per-percentile | Risk-aware budgeting with variance control |
| 6 Model Downgrade Cascade | Per-model cost forecast | None | 70-80% | Cost-capped APIs with quality floors |
