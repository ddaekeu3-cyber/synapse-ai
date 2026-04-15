---
layout: solution
title: "Agent Uses Wrong Model for Task Type — Slow/Expensive Model for Simple Tasks"
category: config
description: "Agent uses claude-opus-4-6 to classify a sentiment (Yes/No decision). Or uses claude-haiku to write a complex multi-step plan that requires deep reasoning. Wrong model selection wastes money on simple tasks or produces poor results on complex ones."
tags: [config, model-routing, model-selection, cost, latency, claude, haiku, sonnet, opus, task-classification]
---

# Agent Uses Wrong Model for Task Type — Slow/Expensive Model for Simple Tasks

## Symptom
- Every API call uses the most expensive model — costs 10x more than necessary
- Agent uses a fast/cheap model for complex multi-step planning — output quality suffers
- Classification task (3 categories) uses Opus — takes 8 seconds and costs $0.15 per call
- Complex code generation uses Haiku — produces incorrect, incomplete code
- Agent hardcodes `model="claude-opus-4-6"` for all calls — no differentiation by task type
- Routing logic doesn't exist — model choice was made once at setup and never revisited

## Root Cause
Most agents are built with a single model ID hardcoded for all tasks. But different tasks have very different requirements: simple classification, extraction, or yes/no decisions need fast and cheap models; complex reasoning, code generation, and multi-step planning need capable models. Without task-based model routing, the agent either over-spends on simple tasks or under-performs on complex ones. The fix is to classify tasks by complexity and route each call to the appropriate model tier.

## Fix

### Option 1: Task-type routing table — map task types to models

```python
import anthropic
from enum import Enum
from dataclasses import dataclass

client = anthropic.Anthropic()

class TaskComplexity(Enum):
    TRIVIAL = "trivial"      # Classification, extraction, yes/no, formatting
    SIMPLE = "simple"        # Summarization, translation, short generation
    STANDARD = "standard"    # Multi-step reasoning, code generation, analysis
    COMPLEX = "complex"      # Architecture design, deep research, novel reasoning

@dataclass
class ModelConfig:
    model_id: str
    max_tokens: int
    description: str

# Model routing table — update when models change
MODEL_ROUTING: dict[TaskComplexity, ModelConfig] = {
    TaskComplexity.TRIVIAL: ModelConfig(
        model_id="claude-haiku-4-5-20251001",
        max_tokens=256,
        description="Fast classification and extraction"
    ),
    TaskComplexity.SIMPLE: ModelConfig(
        model_id="claude-haiku-4-5-20251001",
        max_tokens=1024,
        description="Summarization and short generation"
    ),
    TaskComplexity.STANDARD: ModelConfig(
        model_id="claude-sonnet-4-6",
        max_tokens=4096,
        description="Multi-step reasoning and code generation"
    ),
    TaskComplexity.COMPLEX: ModelConfig(
        model_id="claude-opus-4-6",
        max_tokens=8192,
        description="Deep analysis and architectural decisions"
    ),
}

# Explicit task type assignments — no guessing
TASK_TYPES: dict[str, TaskComplexity] = {
    # Trivial — always use Haiku
    "classify_sentiment": TaskComplexity.TRIVIAL,
    "extract_entities": TaskComplexity.TRIVIAL,
    "is_spam": TaskComplexity.TRIVIAL,
    "categorize_topic": TaskComplexity.TRIVIAL,
    "extract_date": TaskComplexity.TRIVIAL,
    "yes_no_question": TaskComplexity.TRIVIAL,
    "format_output": TaskComplexity.TRIVIAL,

    # Simple — Haiku with more tokens
    "summarize_text": TaskComplexity.SIMPLE,
    "translate": TaskComplexity.SIMPLE,
    "rewrite_tone": TaskComplexity.SIMPLE,
    "extract_key_points": TaskComplexity.SIMPLE,

    # Standard — Sonnet
    "generate_code": TaskComplexity.STANDARD,
    "debug_code": TaskComplexity.STANDARD,
    "explain_concept": TaskComplexity.STANDARD,
    "write_tests": TaskComplexity.STANDARD,
    "analyze_data": TaskComplexity.STANDARD,
    "plan_task": TaskComplexity.STANDARD,

    # Complex — Opus
    "architect_system": TaskComplexity.COMPLEX,
    "deep_research": TaskComplexity.COMPLEX,
    "security_audit": TaskComplexity.COMPLEX,
    "novel_algorithm": TaskComplexity.COMPLEX,
}

def call_with_routing(
    task_type: str,
    prompt: str,
    system: str = None,
    override_complexity: TaskComplexity = None
) -> str:
    """
    Call Claude with the right model for this task type.
    Use override_complexity for one-off adjustments.
    """
    complexity = override_complexity or TASK_TYPES.get(task_type, TaskComplexity.STANDARD)
    config = MODEL_ROUTING[complexity]

    print(f"Task '{task_type}' → {config.model_id} (max_tokens={config.max_tokens})")

    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": config.model_id, "max_tokens": config.max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return response.content[0].text

# Usage:
sentiment = call_with_routing("classify_sentiment", "Rate this review as positive/negative: 'Great product!'")
# → Uses Haiku (~0.001 cost, ~0.3s)

architecture = call_with_routing("architect_system", "Design a distributed event processing system for 10M events/day")
# → Uses Opus (full capability, appropriate cost)
```

### Option 2: Automatic complexity inference — classify before routing

```python
import anthropic
from typing import Optional

client = anthropic.Anthropic()

COMPLEXITY_CLASSIFIER_PROMPT = """Classify the complexity of this AI task into one of four levels:

TRIVIAL: Simple extraction, yes/no decisions, classification into fixed categories, format conversion
SIMPLE: Short generation, summarization, translation, basic rewriting
STANDARD: Code generation, multi-step analysis, debugging, structured planning
COMPLEX: Novel reasoning, architecture design, deep research, tasks requiring sustained creativity

Task to classify: {task}

Reply with exactly one word: TRIVIAL, SIMPLE, STANDARD, or COMPLEX"""

_complexity_cache: dict[str, TaskComplexity] = {}

def infer_task_complexity(task_description: str) -> TaskComplexity:
    """
    Use a cheap model to classify task complexity before routing.
    Results are cached to avoid repeated classification calls.
    """
    cache_key = task_description[:200]
    if cache_key in _complexity_cache:
        return _complexity_cache[cache_key]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Always use cheapest for meta-classification
        max_tokens=20,
        messages=[{
            "role": "user",
            "content": COMPLEXITY_CLASSIFIER_PROMPT.format(task=task_description[:500])
        }]
    )

    label = response.content[0].text.strip().upper()
    complexity_map = {
        "TRIVIAL": TaskComplexity.TRIVIAL,
        "SIMPLE": TaskComplexity.SIMPLE,
        "STANDARD": TaskComplexity.STANDARD,
        "COMPLEX": TaskComplexity.COMPLEX,
    }
    complexity = complexity_map.get(label, TaskComplexity.STANDARD)
    _complexity_cache[cache_key] = complexity

    print(f"Task complexity: {label} → {MODEL_ROUTING[complexity].model_id}")
    return complexity

def call_with_auto_routing(
    task: str,
    prompt: str,
    system: str = None
) -> str:
    """Route automatically based on inferred task complexity"""
    complexity = infer_task_complexity(task)
    config = MODEL_ROUTING[complexity]

    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": config.model_id, "max_tokens": config.max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return response.content[0].text
```

### Option 3: Cost-aware routing with budget tracking

```python
import time
from dataclasses import dataclass, field
from typing import Optional

# Approximate costs per million tokens (update as pricing changes)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class UsageTracker:
    """Track costs by model and task type"""
    _calls: list[dict] = field(default_factory=list)

    def record(self, model: str, task_type: str, input_tokens: int, output_tokens: int):
        costs = MODEL_COSTS.get(model, {"input": 0, "output": 0})
        cost = (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000
        self._calls.append({
            "model": model,
            "task_type": task_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "timestamp": time.time()
        })

    def report(self) -> dict:
        if not self._calls:
            return {}
        total_cost = sum(c["cost_usd"] for c in self._calls)
        by_model = {}
        for call in self._calls:
            m = call["model"]
            by_model.setdefault(m, {"calls": 0, "cost": 0})
            by_model[m]["calls"] += 1
            by_model[m]["cost"] += call["cost_usd"]

        return {
            "total_calls": len(self._calls),
            "total_cost_usd": round(total_cost, 4),
            "by_model": {m: {**v, "cost": round(v["cost"], 4)} for m, v in by_model.items()},
            "potential_savings": self._calculate_savings()
        }

    def _calculate_savings(self) -> dict:
        """Estimate savings if simple tasks used cheaper models"""
        savings = 0
        for call in self._calls:
            if call["model"] == "claude-opus-4-6":
                haiku_cost = (
                    call["input_tokens"] * 0.80 + call["output_tokens"] * 4.00
                ) / 1_000_000
                opus_cost = call["cost_usd"]
                savings += (opus_cost - haiku_cost)
        return {"if_haiku_for_simple": round(savings, 4)}

tracker = UsageTracker()

def call_with_cost_tracking(
    task_type: str,
    prompt: str,
    system: str = None
) -> tuple[str, dict]:
    """Call with routing and cost tracking"""
    complexity = TASK_TYPES.get(task_type, TaskComplexity.STANDARD)
    config = MODEL_ROUTING[complexity]

    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": config.model_id, "max_tokens": config.max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    text = response.content[0].text

    tracker.record(
        model=config.model_id,
        task_type=task_type,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens
    )

    return text, tracker.report()
```

### Option 4: Multi-tier agent pipeline — route by stage

```python
import anthropic

client = anthropic.Anthropic()

class TieredAgentPipeline:
    """
    Multi-stage pipeline where each stage uses the right model tier.
    Stage 1 (Planning): Opus — needs deep reasoning
    Stage 2 (Execution): Sonnet — needs reliable code/text generation
    Stage 3 (Verification): Haiku — needs fast yes/no checks
    Stage 4 (Formatting): Haiku — pure transformation
    """

    def plan(self, task: str, context: str = "") -> str:
        """Use Opus to deeply understand the task and create a plan"""
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            system="You are a senior architect. Create a detailed, step-by-step execution plan.",
            messages=[{"role": "user", "content": f"Task: {task}\nContext: {context}\n\nCreate a numbered execution plan."}]
        )
        return response.content[0].text

    def execute_step(self, step: str, plan_context: str) -> str:
        """Use Sonnet to execute each step from the plan"""
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system="Execute the given step precisely. Output only the result of this step.",
            messages=[{
                "role": "user",
                "content": f"Plan context:\n{plan_context}\n\nExecute this step:\n{step}"
            }]
        )
        return response.content[0].text

    def verify(self, output: str, criterion: str) -> bool:
        """Use Haiku for fast yes/no verification"""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": f"Does this output satisfy the criterion? Reply YES or NO only.\n\nOutput: {output[:1000]}\n\nCriterion: {criterion}"
            }]
        )
        return "YES" in response.content[0].text.upper()

    def format_output(self, raw_output: str, output_format: str) -> str:
        """Use Haiku for pure formatting transformation"""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": f"Reformat the following into {output_format}:\n\n{raw_output}"
            }]
        )
        return response.content[0].text

    def run(self, task: str, output_format: str = "markdown") -> str:
        """Run the full tiered pipeline"""
        print("Stage 1: Planning (Opus)")
        plan = self.plan(task)

        steps = [line for line in plan.split("\n") if line.strip() and line[0].isdigit()]
        results = []

        print(f"Stage 2: Executing {len(steps)} steps (Sonnet)")
        for step in steps:
            result = self.execute_step(step, plan)
            results.append(result)

        combined = "\n\n".join(results)

        print("Stage 3: Verifying (Haiku)")
        is_complete = self.verify(combined, f"The task '{task}' is fully addressed")
        if not is_complete:
            print("Output incomplete — re-executing with full plan context")
            combined = self.execute_step("Complete the full task", f"Task: {task}\nPlan: {plan}")

        print("Stage 4: Formatting (Haiku)")
        return self.format_output(combined, output_format)

pipeline = TieredAgentPipeline()
```

### Option 5: Environment-based model config — different models per environment

```python
import os
from dataclasses import dataclass

@dataclass
class EnvironmentModelConfig:
    """
    Different model choices per environment.
    Development: cheap/fast for iteration speed
    Staging: same as production
    Production: optimized for quality/cost balance
    """
    trivial: str
    simple: str
    standard: str
    complex: str

MODEL_CONFIGS = {
    "development": EnvironmentModelConfig(
        trivial="claude-haiku-4-5-20251001",
        simple="claude-haiku-4-5-20251001",
        standard="claude-haiku-4-5-20251001",  # Use Haiku everywhere in dev for speed/cost
        complex="claude-sonnet-4-6"             # Only use Sonnet for complex in dev
    ),
    "staging": EnvironmentModelConfig(
        trivial="claude-haiku-4-5-20251001",
        simple="claude-haiku-4-5-20251001",
        standard="claude-sonnet-4-6",
        complex="claude-opus-4-6"
    ),
    "production": EnvironmentModelConfig(
        trivial="claude-haiku-4-5-20251001",
        simple="claude-haiku-4-5-20251001",
        standard="claude-sonnet-4-6",
        complex="claude-opus-4-6"
    ),
}

def get_model_for_env(complexity: TaskComplexity) -> str:
    """Get the right model for this complexity level in the current environment"""
    env = os.getenv("AGENT_ENV", "development")
    config = MODEL_CONFIGS.get(env, MODEL_CONFIGS["development"])

    return {
        TaskComplexity.TRIVIAL: config.trivial,
        TaskComplexity.SIMPLE: config.simple,
        TaskComplexity.STANDARD: config.standard,
        TaskComplexity.COMPLEX: config.complex,
    }[complexity]
```

### Option 6: Routing audit — identify misrouted calls in logs

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class RoutingAuditEntry:
    task_type: str
    model_used: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    recommended_model: Optional[str] = None
    potential_savings: float = 0.0

class RoutingAuditor:
    """
    Audit model routing decisions — find over/under-spending patterns.
    Run on production logs weekly to tune the routing table.
    """

    COMPLEXITY_SIGNALS = {
        # Signals that suggest TRIVIAL complexity (Haiku is sufficient)
        "trivial": [
            "classify", "extract", "yes or no", "true or false",
            "sentiment", "category", "label", "tag", "format as"
        ],
        # Signals that suggest COMPLEX (Opus is warranted)
        "complex": [
            "architect", "design system", "security audit", "novel approach",
            "comprehensive analysis", "detailed research", "complex algorithm"
        ]
    }

    def audit_call(
        self,
        task_description: str,
        model_used: str,
        input_tokens: int,
        output_tokens: int
    ) -> RoutingAuditEntry:
        costs = MODEL_COSTS.get(model_used, {"input": 0, "output": 0})
        actual_cost = (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000

        # Check if this looks misrouted
        task_lower = task_description.lower()
        is_trivial_task = any(s in task_lower for s in self.COMPLEXITY_SIGNALS["trivial"])
        is_complex_task = any(s in task_lower for s in self.COMPLEXITY_SIGNALS["complex"])

        recommended = None
        savings = 0.0

        if is_trivial_task and model_used != "claude-haiku-4-5-20251001":
            recommended = "claude-haiku-4-5-20251001"
            haiku_cost = (input_tokens * 0.80 + output_tokens * 4.00) / 1_000_000
            savings = actual_cost - haiku_cost

        elif is_complex_task and model_used == "claude-haiku-4-5-20251001":
            recommended = "claude-opus-4-6"
            # No savings — it costs more, but quality matters here

        return RoutingAuditEntry(
            task_type=task_description[:80],
            model_used=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=actual_cost,
            recommended_model=recommended,
            potential_savings=savings
        )

    def generate_report(self, entries: list[RoutingAuditEntry]) -> dict:
        misrouted = [e for e in entries if e.recommended_model]
        total_savings = sum(e.potential_savings for e in misrouted)
        return {
            "total_calls": len(entries),
            "misrouted_calls": len(misrouted),
            "potential_savings_usd": round(total_savings, 4),
            "misrouted_examples": [
                f"'{e.task_type}' used {e.model_used} — should use {e.recommended_model} (save ${e.potential_savings:.4f})"
                for e in misrouted[:5]
            ]
        }

auditor = RoutingAuditor()
```

## Model Selection by Task Type

| Task Category | Examples | Recommended Model | Rationale |
|---|---|---|---|
| Binary decisions | Spam detection, sentiment, yes/no | Haiku | Single-token answer |
| Extraction | Entity extraction, date parsing, field extraction | Haiku | Pattern matching |
| Short generation | Titles, labels, tags, short descriptions | Haiku | Low complexity |
| Summarization | Document summary, key points | Haiku | Compression task |
| Code generation | Functions, classes, modules | Sonnet | Reliable reasoning |
| Debugging | Root cause analysis, fix suggestion | Sonnet | Multi-step logic |
| Planning | Task breakdown, sequence design | Sonnet | Structured output |
| Architecture | System design, security audit | Opus | Novel reasoning |
| Deep research | Comprehensive analysis, synthesis | Opus | Sustained complexity |

## Expected Token Savings
All tasks on Opus: 100% of potential cost spent
Routed by task type: ~70% cost reduction (most tasks are trivial/simple)
Example: 1M calls/day → 95% Haiku + 4% Sonnet + 1% Opus = ~85% cost savings vs all-Opus

## Environment
- Any production agent with mixed task types; model routing is the highest-leverage cost optimization available — a single routing table can reduce API costs by 50-90% without any loss of output quality for complex tasks
- Source: direct experience; hardcoded model selection is universal in early-stage agents and universally replaced with routing tables within the first month of production operation
