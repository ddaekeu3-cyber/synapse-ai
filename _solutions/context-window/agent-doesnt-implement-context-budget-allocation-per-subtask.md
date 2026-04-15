---
layout: solution
title: "Agent Doesn't Implement Context Budget Allocation Per Subtask"
category: context-window
description: "Multi-step agents that share a single unbounded context window across all subtasks run out of space late in pipelines, causing truncation or expensive model calls."
tags: [context-window, budget, multi-step, pipeline, token-management, planning]
---

# Agent Doesn't Implement Context Budget Allocation Per Subtask

In multi-step agentic pipelines, each subtask contributes messages, tool results, and intermediate reasoning to a shared context window. Without explicit budgeting, early steps consume disproportionate space and late-stage subtasks receive a truncated, polluted context — or the agent hits the 200K token ceiling mid-task.

## Why This Happens

Agents are often written one step at a time, with each step blindly appending to `messages`. There's no upfront plan for how many tokens each step should consume, no enforcement of per-step limits, and no pruning strategy when a step exceeds its allocation.

---

## Option 1: Static Budget Allocation with Enforcement

Define a fixed token budget per subtask type and raise an error if a step tries to exceed it.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

MODEL_CONTEXT_LIMIT = 200_000
RESPONSE_RESERVE = 4_096  # tokens reserved for output


@dataclass
class SubtaskBudget:
    name: str
    max_input_tokens: int


# Allocate context budget across pipeline stages
PIPELINE_BUDGETS = [
    SubtaskBudget("research",      40_000),
    SubtaskBudget("analysis",      30_000),
    SubtaskBudget("drafting",      20_000),
    SubtaskBudget("review",        10_000),
    SubtaskBudget("final_output",   8_000),
]


def count_message_tokens(messages: list[dict], client: anthropic.Anthropic) -> int:
    """Use the count_tokens API to get exact token count."""
    response = client.messages.count_tokens(
        model="claude-sonnet-4-6",
        messages=messages,
    )
    return response.input_tokens


def run_subtask(
    name: str,
    budget: SubtaskBudget,
    messages: list[dict],
    system: str = "",
) -> str:
    token_count = count_message_tokens(messages, client)

    if token_count > budget.max_input_tokens:
        raise ValueError(
            f"Subtask '{name}' exceeds budget: "
            f"{token_count} > {budget.max_input_tokens} tokens"
        )

    kwargs = {}
    if system:
        kwargs["system"] = system

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=RESPONSE_RESERVE,
        messages=messages,
        **kwargs,
    )
    return response.content[0].text


# Multi-step pipeline with budgeted stages
def run_pipeline(topic: str) -> str:
    budgets = {b.name: b for b in PIPELINE_BUDGETS}

    # Stage 1: Research
    research_messages = [{"role": "user", "content": f"Research key facts about: {topic}"}]
    research_result = run_subtask("research", budgets["research"], research_messages)

    # Stage 2: Analysis (only pass research summary, not full conversation)
    analysis_messages = [
        {"role": "user", "content": f"Analyze this research:\n\n{research_result[:8000]}"}
    ]
    analysis_result = run_subtask("analysis", budgets["analysis"], analysis_messages)

    # Stage 3: Final output
    final_messages = [
        {
            "role": "user",
            "content": (
                f"Write a concise report based on:\n\n"
                f"Research: {research_result[:3000]}\n\n"
                f"Analysis: {analysis_result[:3000]}"
            ),
        }
    ]
    return run_subtask("final_output", budgets["final_output"], final_messages)


if __name__ == "__main__":
    report = run_pipeline("renewable energy trends 2025")
    print(report)
```

**Expected Token Savings:** Enforces discipline on each stage; prevents one verbose step from consuming tokens needed by later steps.

**Environment:** Linear multi-step pipelines; Claude Sonnet/Opus for complex tasks.

---

## Option 2: Dynamic Budget Reallocation

Measure actual token usage after each step and redistribute remaining budget dynamically.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class ContextBudgetManager:
    total_budget: int = 180_000  # leave headroom from 200K
    reserved_output: int = 8_000
    steps: list[str] = field(default_factory=list)
    step_weights: dict[str, float] = field(default_factory=dict)
    consumed: dict[str, int] = field(default_factory=dict)

    @property
    def input_budget(self) -> int:
        return self.total_budget - self.reserved_output

    def allocate(self, step: str) -> int:
        """Compute dynamic allocation based on remaining budget and weights."""
        spent = sum(self.consumed.values())
        remaining = self.input_budget - spent

        remaining_steps = [s for s in self.steps if s not in self.consumed]
        if not remaining_steps:
            return remaining

        total_weight = sum(self.step_weights.get(s, 1.0) for s in remaining_steps)
        step_weight = self.step_weights.get(step, 1.0)
        return int(remaining * step_weight / total_weight)

    def record_usage(self, step: str, tokens_used: int):
        self.consumed[step] = tokens_used
        print(
            f"  [{step}] used {tokens_used:,} tokens | "
            f"remaining: {self.input_budget - sum(self.consumed.values()):,}"
        )


def run_with_budget(
    budget_mgr: ContextBudgetManager,
    step: str,
    messages: list[dict],
) -> tuple[str, int]:
    allocation = budget_mgr.allocate(step)

    # Count actual tokens
    token_resp = client.messages.count_tokens(
        model="claude-sonnet-4-6",
        messages=messages,
    )
    actual_tokens = token_resp.input_tokens

    if actual_tokens > allocation:
        # Trim last message to fit
        overage = actual_tokens - allocation
        last_content = messages[-1]["content"]
        # Rough trim: ~4 chars per token
        trim_chars = overage * 4
        messages[-1]["content"] = last_content[: max(100, len(last_content) - trim_chars)]
        print(f"  [{step}] TRIMMED by ~{trim_chars} chars to fit budget")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=budget_mgr.reserved_output,
        messages=messages,
    )
    budget_mgr.record_usage(step, actual_tokens)
    return response.content[0].text, actual_tokens


# Usage
def dynamic_pipeline(document: str) -> str:
    mgr = ContextBudgetManager(
        steps=["extract", "summarize", "qa", "report"],
        step_weights={"extract": 2.0, "summarize": 1.5, "qa": 1.0, "report": 1.0},
    )

    extract_result, _ = run_with_budget(
        mgr, "extract",
        [{"role": "user", "content": f"Extract key entities from:\n\n{document}"}],
    )

    summary_result, _ = run_with_budget(
        mgr, "summarize",
        [{"role": "user", "content": f"Summarize this document:\n\n{document[:20000]}"}],
    )

    qa_result, _ = run_with_budget(
        mgr, "qa",
        [{"role": "user", "content": f"List 5 key questions about:\n{summary_result}"}],
    )

    report, _ = run_with_budget(
        mgr, "report",
        [{"role": "user", "content": f"Write report. Entities: {extract_result[:1000]}\nSummary: {summary_result[:2000]}\nQuestions: {qa_result}"}],
    )
    return report
```

**Expected Token Savings:** Adapts to actual usage; early-stage brevity frees tokens for complex late stages.

**Environment:** Variable-length input pipelines where step sizes are unpredictable.

---

## Option 3: Isolated Context Per Subtask (Stateless Steps)

Each subtask gets a fresh context containing only its specific inputs, not the full conversation history.

```python
import anthropic
from typing import Callable

client = anthropic.Anthropic()

SubtaskFn = Callable[[dict], str]


def make_isolated_step(
    name: str,
    system_prompt: str,
    max_input_tokens: int = 20_000,
    model: str = "claude-haiku-4-5-20251001",
) -> SubtaskFn:
    """Factory that creates a stateless subtask function with its own context."""

    def step(inputs: dict) -> str:
        # Build a fresh, minimal context for this step only
        user_content = "\n\n".join(
            f"**{k.upper()}**:\n{str(v)[:max_input_tokens // len(inputs)]}"
            for k, v in inputs.items()
        )

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text

    step.__name__ = name
    return step


# Define isolated pipeline steps
extract_facts = make_isolated_step(
    "extract_facts",
    "Extract factual claims as a numbered list. Be concise.",
    max_input_tokens=50_000,
    model="claude-haiku-4-5-20251001",
)

evaluate_claims = make_isolated_step(
    "evaluate_claims",
    "Rate each claim as VERIFIED, UNCERTAIN, or UNVERIFIED. One line per claim.",
    max_input_tokens=10_000,
    model="claude-haiku-4-5-20251001",
)

write_report = make_isolated_step(
    "write_report",
    "Write a structured executive summary (max 500 words).",
    max_input_tokens=8_000,
    model="claude-sonnet-4-6",
)


def isolated_pipeline(raw_document: str) -> str:
    facts = extract_facts({"document": raw_document})
    evaluations = evaluate_claims({"claims": facts})
    report = write_report({"facts": facts[:2000], "evaluations": evaluations[:2000]})
    return report


if __name__ == "__main__":
    doc = "..." * 1000  # long document
    print(isolated_pipeline(doc))
```

**Expected Token Savings:** Each step only sees its relevant inputs; eliminates cumulative context growth entirely; 60–80% token reduction versus a shared conversation.

**Environment:** Pipelines where steps are functionally independent; best for ETL-style agent flows.

---

## Option 4: Context Window Planning Phase

Add an explicit planning step that estimates token requirements for each stage before execution.

```python
import json
import anthropic

client = anthropic.Anthropic()

CONTEXT_LIMIT = 200_000
OUTPUT_RESERVE = 8_000
AVAILABLE = CONTEXT_LIMIT - OUTPUT_RESERVE


def plan_context_allocation(task_description: str, num_steps: int) -> dict[str, int]:
    """Ask the model to plan token allocations before execution."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    f"I have {AVAILABLE:,} tokens available for a {num_steps}-step pipeline.\n"
                    f"Task: {task_description}\n\n"
                    f"Return a JSON object mapping step names to integer token budgets. "
                    f"The budgets must sum to <= {AVAILABLE}. "
                    f"Example: {{\"step1\": 30000, \"step2\": 20000}}"
                ),
            }
        ],
    )

    raw = response.content[0].text
    # Extract JSON from response
    start = raw.find("{")
    end = raw.rfind("}") + 1
    allocations: dict[str, int] = json.loads(raw[start:end])

    # Normalize to ensure we don't exceed available budget
    total = sum(allocations.values())
    if total > AVAILABLE:
        scale = AVAILABLE / total
        allocations = {k: int(v * scale) for k, v in allocations.items()}

    return allocations


def execute_with_planned_budget(task: str, steps: list[tuple[str, str]]) -> list[str]:
    """
    steps: list of (step_name, prompt_template) tuples
    prompt_template can include {prev_output} placeholder
    """
    allocations = plan_context_allocation(task, len(steps))
    print("Planned allocations:", allocations)

    results = []
    prev_output = ""

    for step_name, prompt_template in steps:
        budget = allocations.get(step_name, AVAILABLE // len(steps))
        prompt = prompt_template.format(
            prev_output=prev_output[:budget // 2],
            task=task,
        )

        # Verify we're within budget
        token_count = client.messages.count_tokens(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": prompt}],
        ).input_tokens

        if token_count > budget:
            # Trim proportionally
            ratio = budget / token_count
            prompt = prompt[: int(len(prompt) * ratio)]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        prev_output = response.content[0].text
        results.append(prev_output)
        print(f"  [{step_name}] {token_count:,} tokens used / {budget:,} budgeted")

    return results


# Usage
if __name__ == "__main__":
    pipeline_steps = [
        ("research", "Research the topic: {task}"),
        ("synthesize", "Synthesize this research into key insights:\n\n{prev_output}"),
        ("report", "Write a final report based on:\n\n{prev_output}"),
    ]
    outputs = execute_with_planned_budget(
        "impact of LLMs on software engineering productivity",
        pipeline_steps,
    )
    print("\nFinal output:\n", outputs[-1])
```

**Expected Token Savings:** Model-assisted planning produces realistic budgets; prevents 2x over-allocation and ensures last steps have adequate context.

**Environment:** Complex pipelines where step input sizes are variable and hard to predict statically.

---

## Option 5: Sliding Summary Window

Instead of keeping raw outputs, compress each step's result into a summary before passing to the next step.

```python
import anthropic

client = anthropic.Anthropic()

MAX_PASSTHROUGH_TOKENS = 3_000  # tokens to pass from one step to the next


def compress_to_budget(text: str, target_tokens: int) -> str:
    """Compress text to approximately target_tokens using the model."""
    # Rough estimate: 1 token ≈ 4 chars
    if len(text) <= target_tokens * 4:
        return text  # already small enough

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=target_tokens,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Compress the following to ~{target_tokens} tokens "
                    f"while preserving all key information:\n\n{text[:50000]}"
                ),
            }
        ],
    )
    return response.content[0].text


def sliding_window_pipeline(document: str, questions: list[str]) -> list[str]:
    """Answer each question about a document, compressing context between steps."""
    # Initial document summary to fit in budget
    compressed_doc = compress_to_budget(document, MAX_PASSTHROUGH_TOKENS)
    print(f"Document compressed: {len(document)} chars -> {len(compressed_doc)} chars")

    answers = []
    running_context = compressed_doc

    for i, question in enumerate(questions):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Context:\n{running_context}\n\n"
                        f"Question {i+1}: {question}"
                    ),
                }
            ],
        )
        answer = response.content[0].text
        answers.append(answer)

        # Compress running context for next step
        new_context = f"{running_context}\n\nQ{i+1}: {question}\nA: {answer}"
        if len(new_context) > MAX_PASSTHROUGH_TOKENS * 4:
            running_context = compress_to_budget(new_context, MAX_PASSTHROUGH_TOKENS)
        else:
            running_context = new_context

    return answers


if __name__ == "__main__":
    long_doc = "This is a long research paper..." * 500
    qs = [
        "What is the main thesis?",
        "What evidence supports it?",
        "What are the limitations?",
    ]
    for q, a in zip(qs, sliding_window_pipeline(long_doc, qs)):
        print(f"Q: {q}\nA: {a}\n")
```

**Expected Token Savings:** Context grows sub-linearly with pipeline length; 70%+ reduction in token cost for 10+ step pipelines.

**Environment:** Long multi-turn Q&A over documents; iterative refinement pipelines.

---

## Option 6: Per-Agent Context Quota with Async Parallel Steps

In parallel multi-agent pipelines, assign each agent a context quota and enforce it with a semaphore.

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class AgentQuota:
    agent_id: str
    max_tokens: int
    used_tokens: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    def consume(self, tokens: int):
        self.used_tokens += tokens
        if self.used_tokens > self.max_tokens:
            raise RuntimeError(
                f"Agent {self.agent_id} exceeded token quota: "
                f"{self.used_tokens} > {self.max_tokens}"
            )


async def run_agent(
    quota: AgentQuota,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    # Check available budget before calling
    token_count_resp = await client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    input_tokens = token_count_resp.input_tokens

    if input_tokens > quota.remaining:
        # Trim prompt to fit
        ratio = quota.remaining / max(1, input_tokens)
        prompt = prompt[: int(len(prompt) * ratio)]
        print(f"  [Agent {quota.agent_id}] TRIMMED prompt to fit {quota.remaining} token budget")

    response = await client.messages.create(
        model=model,
        max_tokens=min(2048, quota.remaining),
        messages=[{"role": "user", "content": prompt}],
    )

    output_tokens = response.usage.output_tokens
    quota.consume(input_tokens + output_tokens)
    return response.content[0].text


async def parallel_pipeline(document: str) -> dict[str, str]:
    """Run 4 analysis agents in parallel, each with an isolated token quota."""
    total_budget = 100_000
    per_agent_budget = total_budget // 4

    quotas = {
        "summarizer": AgentQuota("summarizer", per_agent_budget),
        "fact_extractor": AgentQuota("fact_extractor", per_agent_budget),
        "sentiment": AgentQuota("sentiment", per_agent_budget),
        "classifier": AgentQuota("classifier", per_agent_budget),
    }

    tasks = {
        "summarizer": run_agent(quotas["summarizer"], f"Summarize:\n{document[:10000]}"),
        "fact_extractor": run_agent(quotas["fact_extractor"], f"Extract facts:\n{document[:10000]}"),
        "sentiment": run_agent(quotas["sentiment"], f"Analyze sentiment:\n{document[:5000]}"),
        "classifier": run_agent(quotas["classifier"], f"Classify document type:\n{document[:3000]}"),
    }

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    output = {}
    for agent_id, result in zip(tasks.keys(), results):
        output[agent_id] = str(result) if isinstance(result, Exception) else result
        print(f"  [Agent {agent_id}] used {quotas[agent_id].used_tokens:,} / {per_agent_budget:,} tokens")

    return output


if __name__ == "__main__":
    doc = "Long document content..." * 200
    results = asyncio.run(parallel_pipeline(doc))
    for agent, output in results.items():
        print(f"\n[{agent}]\n{output[:200]}")
```

**Expected Token Savings:** Parallel agents each respect their quota; total cost is bounded and predictable regardless of document length.

**Environment:** Fan-out multi-agent architectures; async pipelines with parallel subtasks.

---

## Comparison

| Option | Budget Type | Context Isolation | Adaptive | Multi-Agent |
|--------|-------------|-------------------|----------|-------------|
| 1. Static allocation | Fixed per step | Partial | No | No |
| 2. Dynamic reallocation | Remaining budget split | Partial | Yes | No |
| 3. Isolated steps | Full isolation | Complete | No | No |
| 4. Planning phase | Model-advised | Partial | Yes | No |
| 5. Sliding summary | Compressed passthrough | Compression | Yes | No |
| 6. Per-agent quota | Per-agent hard cap | Complete | No | Yes |
