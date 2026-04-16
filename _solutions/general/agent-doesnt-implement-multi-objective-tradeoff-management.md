---
title: "Agent Doesn't Implement Multi-Objective Tradeoff Management"
description: "Real tasks involve competing objectives — speed vs. quality, cost vs. accuracy, brevity vs. completeness. Agents that optimize for a single metric fail on all others. Multi-objective tradeoff management lets agents balance competing goals explicitly and transparently."
difficulty: advanced
category: general
tags: [multi-objective, tradeoffs, optimization, pareto, cost-quality, decision-making]
---

## Problem

An agent asked to "summarize this document" has multiple competing objectives: completeness (keep all important details), brevity (be short), accuracy (don't distort), and speed (respond quickly). Without explicit tradeoff management, the agent picks an arbitrary balance, usually the same one every time regardless of what the user actually needs. When the user says "make it shorter" or "I need this fast," the agent has no principled way to re-weight objectives.

```python
# BAD: implicit single objective — always optimize for one thing
async def summarize(document: str) -> str:
    return await call_model(
        f"Summarize this document:\n\n{document}"
    )
# No way to express "shorter but less complete" vs "complete but longer"
```

## Solution 1: Explicit Objective Weights

Let callers specify weights for competing objectives; adjust prompt accordingly.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class ObjectiveWeights:
    """Weights must sum to ~1.0. Higher = more important."""
    completeness: float = 0.4   # how much detail to preserve
    brevity: float = 0.3        # how short to be
    accuracy: float = 0.2       # factual precision
    readability: float = 0.1    # ease of reading

    def validate(self):
        total = self.completeness + self.brevity + self.accuracy + self.readability
        assert 0.9 <= total <= 1.1, f"Weights should sum to ~1.0, got {total:.2f}"

def weights_to_instructions(w: ObjectiveWeights) -> str:
    instructions = []

    if w.completeness > 0.5:
        instructions.append("Be comprehensive — include all key points even if it makes the summary longer.")
    elif w.completeness < 0.2:
        instructions.append("Only the single most important takeaway. Drop everything else.")
    else:
        instructions.append("Cover main points but skip supporting details.")

    if w.brevity > 0.5:
        instructions.append("Aggressively cut length. Use bullet points. Every word must earn its place.")
    elif w.brevity < 0.2:
        instructions.append("Length is fine. Prioritize thoroughness over concision.")
    else:
        instructions.append("Aim for moderate length — a few paragraphs at most.")

    if w.accuracy > 0.4:
        instructions.append("Never paraphrase in ways that could change meaning. Use exact terms from the source.")
    else:
        instructions.append("Paraphrasing for clarity is acceptable.")

    if w.readability > 0.3:
        instructions.append("Write for a general audience. Avoid jargon. Use simple sentence structure.")

    return "\n".join(f"- {i}" for i in instructions)

async def summarize_with_objectives(
    document: str,
    weights: ObjectiveWeights | None = None
) -> str:
    weights = weights or ObjectiveWeights()
    weights.validate()
    instructions = weights_to_instructions(weights)

    system = f"""You are a document summarizer. Apply these objectives (in priority order):
{instructions}

Tradeoff profile:
- Completeness priority: {weights.completeness:.0%}
- Brevity priority: {weights.brevity:.0%}
- Accuracy priority: {weights.accuracy:.0%}
- Readability priority: {weights.readability:.0%}"""

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": f"Summarize:\n\n{document}"}]
    )
    return response.content[0].text if response.content else ""

async def main():
    doc = """
    The transformer architecture revolutionized natural language processing by replacing
    recurrent networks with attention mechanisms. Self-attention allows the model to weigh
    relationships between all tokens simultaneously, enabling better long-range dependency
    capture. The key innovation was multi-head attention, which lets the model attend to
    different aspects of the sequence in parallel. Positional encodings preserve order
    information since attention is permutation-invariant. Transformers train efficiently
    on GPUs due to their parallelizable structure, unlike RNNs which process tokens sequentially.
    """

    # Executive briefing: brief + readable
    result = await summarize_with_objectives(doc, ObjectiveWeights(
        completeness=0.1, brevity=0.6, accuracy=0.1, readability=0.2
    ))
    print(f"[Brief]:\n{result}\n")

    # Technical reference: complete + accurate
    result = await summarize_with_objectives(doc, ObjectiveWeights(
        completeness=0.5, brevity=0.1, accuracy=0.3, readability=0.1
    ))
    print(f"[Technical]:\n{result}")

asyncio.run(main())
```

## Solution 2: Pareto Frontier Sampling

Generate multiple solutions along the Pareto frontier and let the user choose.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class ParetoOption:
    label: str
    description: str
    prompt_modifier: str
    estimated_tokens: int

PARETO_OPTIONS = [
    ParetoOption(
        label="ultra_brief",
        description="1-2 sentences. Maximum information loss, minimum length.",
        prompt_modifier="Respond in 1-2 sentences only. Use the fewest words possible.",
        estimated_tokens=50
    ),
    ParetoOption(
        label="brief",
        description="3-5 bullet points. Key facts only.",
        prompt_modifier="Respond with exactly 3-5 bullet points. Each bullet: one key fact, one line.",
        estimated_tokens=150
    ),
    ParetoOption(
        label="balanced",
        description="Short paragraph. Main points with minimal context.",
        prompt_modifier="Respond in 2-3 short paragraphs covering the main points.",
        estimated_tokens=300
    ),
    ParetoOption(
        label="complete",
        description="Full detail. All important points preserved.",
        prompt_modifier="Respond comprehensively. Preserve all important details, examples, and nuance.",
        estimated_tokens=600
    ),
]

async def generate_pareto_option(task: str, option: ParetoOption) -> tuple[ParetoOption, str]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=option.estimated_tokens + 100,
        messages=[{
            "role": "user",
            "content": f"{option.prompt_modifier}\n\nTask: {task}"
        }]
    )
    output = response.content[0].text if response.content else ""
    return option, output

async def pareto_sample(task: str, num_options: int = 3) -> list[tuple[ParetoOption, str]]:
    """Generate solutions along the Pareto frontier in parallel."""
    selected = PARETO_OPTIONS[:num_options]
    results = await asyncio.gather(*[
        generate_pareto_option(task, opt) for opt in selected
    ])
    return list(results)

async def main():
    task = "Explain how gradient descent works in machine learning"
    options = await pareto_sample(task, num_options=4)

    print(f"Pareto options for: '{task}'\n")
    for option, output in options:
        print(f"[{option.label.upper()}] — {option.description}")
        print(f"  {output[:200]}\n")

asyncio.run(main())
```

## Solution 3: Dynamic Objective Re-Weighting from User Feedback

Adjust objective weights based on user feedback signals during a session.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

FEEDBACK_ADJUSTMENTS = {
    "too long": {"brevity": +0.2, "completeness": -0.15},
    "too short": {"brevity": -0.2, "completeness": +0.15},
    "too vague": {"completeness": +0.15, "accuracy": +0.1, "brevity": -0.1},
    "too detailed": {"completeness": -0.2, "brevity": +0.15},
    "confusing": {"readability": +0.2, "brevity": -0.05},
    "inaccurate": {"accuracy": +0.25, "completeness": +0.05},
    "good": {},  # no adjustment
}

@dataclass
class AdaptiveObjectives:
    weights: dict[str, float] = field(default_factory=lambda: {
        "completeness": 0.35,
        "brevity": 0.30,
        "accuracy": 0.20,
        "readability": 0.15,
    })
    history: list[str] = field(default_factory=list)

    def apply_feedback(self, feedback: str) -> bool:
        feedback_lower = feedback.lower()
        for signal, adjustments in FEEDBACK_ADJUSTMENTS.items():
            if signal in feedback_lower:
                for objective, delta in adjustments.items():
                    self.weights[objective] = max(0.05, min(0.85,
                        self.weights[objective] + delta
                    ))
                self.history.append(f"Feedback '{signal}' → {adjustments}")
                # Re-normalize
                total = sum(self.weights.values())
                self.weights = {k: v / total for k, v in self.weights.items()}
                return True
        return False

    def to_prompt_fragment(self) -> str:
        sorted_objectives = sorted(self.weights.items(), key=lambda x: x[1], reverse=True)
        return ", ".join(f"{k} ({v:.0%})" for k, v in sorted_objectives)

async def adaptive_response(objectives: AdaptiveObjectives, task: str) -> str:
    obj_description = objectives.to_prompt_fragment()
    system = (
        f"Optimize your response for these objectives in order of importance: {obj_description}\n"
        f"Apply the weights literally — if brevity is 50%, be very short."
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": task}]
    )
    return response.content[0].text if response.content else ""

async def main():
    objectives = AdaptiveObjectives()
    task = "Explain what a REST API is"

    print("=== Initial response ===")
    result = await adaptive_response(objectives, task)
    print(result[:300])

    print("\n=== User says: 'too long' ===")
    objectives.apply_feedback("too long")
    print(f"New weights: {objectives.to_prompt_fragment()}")
    result = await adaptive_response(objectives, task)
    print(result[:300])

    print("\n=== User says: 'too vague' ===")
    objectives.apply_feedback("too vague")
    print(f"New weights: {objectives.to_prompt_fragment()}")
    result = await adaptive_response(objectives, task)
    print(result[:300])

asyncio.run(main())
```

## Solution 4: Cost-Quality Tradeoff Controller

Explicitly manage the cost vs. output quality tradeoff with a budget parameter.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class CostQualityConfig:
    """budget: 0.0 = minimum cost, 1.0 = maximum quality"""
    budget: float  # 0.0 to 1.0

    @property
    def model(self) -> str:
        if self.budget >= 0.8:
            return "claude-sonnet-4-6"
        elif self.budget >= 0.4:
            return "claude-haiku-4-5-20251001"
        else:
            return "claude-haiku-4-5-20251001"  # always use haiku but constrain tokens

    @property
    def max_tokens(self) -> int:
        # Scale tokens 128 (budget=0) to 4096 (budget=1)
        return int(128 + self.budget * (4096 - 128))

    @property
    def prompt_constraint(self) -> str:
        if self.budget < 0.3:
            return "Be extremely brief. Maximum 2-3 sentences. Essential information only."
        elif self.budget < 0.6:
            return "Be concise. A short paragraph. Skip examples unless critical."
        elif self.budget < 0.8:
            return "Be thorough. Cover main points with one example each."
        else:
            return "Be comprehensive. Full explanation with examples, edge cases, and nuance."

async def run_with_cost_quality_budget(task: str, budget: float) -> dict:
    config = CostQualityConfig(budget=max(0.0, min(1.0, budget)))

    response = await client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=config.prompt_constraint,
        messages=[{"role": "user", "content": task}]
    )
    output = response.content[0].text if response.content else ""
    actual_tokens = response.usage.output_tokens

    return {
        "budget": budget,
        "model": config.model,
        "max_tokens_allowed": config.max_tokens,
        "tokens_used": actual_tokens,
        "output_length": len(output),
        "output": output
    }

async def main():
    task = "How does TLS/SSL work?"
    budgets = [0.1, 0.5, 0.9]

    results = await asyncio.gather(*[
        run_with_cost_quality_budget(task, b) for b in budgets
    ])

    for r in results:
        print(f"\n[Budget {r['budget']:.0%}] model={r['model']}, tokens={r['tokens_used']}")
        print(r["output"][:250])

asyncio.run(main())
```

## Solution 5: Constraint Satisfaction with Soft and Hard Objectives

Distinguish between hard constraints (must satisfy) and soft objectives (optimize when possible).

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class HardConstraint:
    name: str
    description: str
    enforcement: str  # instruction to include in prompt

@dataclass
class SoftObjective:
    name: str
    description: str
    weight: float
    instruction: str

def build_constrained_system_prompt(
    hard_constraints: list[HardConstraint],
    soft_objectives: list[SoftObjective]
) -> str:
    hard_section = "\n".join(
        f"MUST: {c.enforcement}" for c in hard_constraints
    )
    sorted_objectives = sorted(soft_objectives, key=lambda o: o.weight, reverse=True)
    soft_section = "\n".join(
        f"PREFER ({o.weight:.0%}): {o.instruction}" for o in sorted_objectives
    )
    return (
        f"You must satisfy ALL of the following hard constraints:\n{hard_section}\n\n"
        f"Additionally, optimize for these objectives (higher % = more important):\n{soft_section}"
    )

async def run_constrained_task(
    task: str,
    hard_constraints: list[HardConstraint],
    soft_objectives: list[SoftObjective]
) -> str:
    system = build_constrained_system_prompt(hard_constraints, soft_objectives)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": task}]
    )
    return response.content[0].text if response.content else ""

async def main():
    hard = [
        HardConstraint("safe", "No medical advice", "Never provide specific medical advice or diagnoses"),
        HardConstraint("accurate", "No misinformation", "Only state facts you are confident about; say 'I'm not sure' otherwise"),
    ]
    soft = [
        SoftObjective("brevity", "Short response", 0.5, "Use the fewest words possible"),
        SoftObjective("examples", "Concrete examples", 0.3, "Include at least one concrete real-world example"),
        SoftObjective("actionable", "Actionable advice", 0.2, "End with a specific action the user can take"),
    ]

    result = await run_constrained_task(
        "What should I know about managing stress?",
        hard, soft
    )
    print(result[:400])

asyncio.run(main())
```

## Solution 6: Multi-Objective Score Reporter

After generating output, score it against each objective and report the tradeoff profile.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SCORER_PROMPT = """Score this text against each objective from 0.0 to 1.0. Output JSON only.

Objectives:
- completeness: Does it cover all important points?
- brevity: Is it as short as it could be while still making sense?
- accuracy: Are the facts correct and precisely stated?
- readability: Is it easy to understand for a general audience?
- actionability: Does it include concrete next steps or examples?

Output format:
{"completeness": 0.0-1.0, "brevity": 0.0-1.0, "accuracy": 0.0-1.0, "readability": 0.0-1.0, "actionability": 0.0-1.0, "overall": 0.0-1.0}"""

async def score_output(output: str) -> dict[str, float]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=SCORER_PROMPT,
        messages=[{"role": "user", "content": f"Text to score:\n{output[:800]}"}]
    )
    text = response.content[0].text.strip()
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {}

async def generate_and_score(task: str, system_prompt: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": task}]
    )
    output = response.content[0].text if response.content else ""
    scores = await score_output(output)
    return {"output": output, "scores": scores, "length": len(output)}

async def compare_tradeoffs(task: str) -> None:
    configs = [
        ("speed_focused", "Be extremely brief. Answer in one or two sentences maximum."),
        ("quality_focused", "Be thorough and precise. Include examples and explain your reasoning."),
        ("balanced", "Balance brevity with completeness. Cover the essentials clearly."),
    ]

    results = await asyncio.gather(*[
        generate_and_score(task, system) for _, system in configs
    ])

    print(f"Task: {task}\n")
    print(f"{'Config':<20} {'Complete':>9} {'Brief':>7} {'Accurate':>9} {'Readable':>9} {'Length':>8}")
    print("-" * 65)
    for (name, _), result in zip(configs, results):
        s = result["scores"]
        print(
            f"{name:<20} "
            f"{s.get('completeness', 0):.2f}{'':>5} "
            f"{s.get('brevity', 0):.2f}{'':>3} "
            f"{s.get('accuracy', 0):.2f}{'':>6} "
            f"{s.get('readability', 0):.2f}{'':>6} "
            f"{result['length']:>8}"
        )

async def main():
    await compare_tradeoffs("What is containerization and why is it useful?")

asyncio.run(main())
```

## Comparison

| Approach | Explicitness | User Control | Latency Added | Best For |
|---|---|---|---|---|
| Explicit Weights | High | Full | None | API consumers with known preferences |
| Pareto Sampling | High | Choice at output | N parallel calls | UX where user picks variant |
| Dynamic Re-weighting | Medium | Via feedback | None | Long chat sessions |
| Cost-Quality Budget | Medium | Single knob | None | Cost-sensitive production |
| Hard + Soft Constraints | Very High | Structured | None | Compliance-sensitive tasks |
| Score Reporter | High | None (observability) | +1 scoring call | Evaluation and monitoring |

**Rule of thumb**: Start with explicit weights for API callers who know their needs. Add Pareto sampling for consumer-facing products where users can't quantify their preferences. Use the score reporter to detect when your default weighting is systematically wrong.
