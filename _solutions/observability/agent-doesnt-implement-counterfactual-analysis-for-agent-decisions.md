---
layout: solution
title: "Agent Doesn't Implement Counterfactual Analysis for Agent Decisions"
description: "How to replay agent decisions with alternative inputs or configurations to understand what would have happened differently — enabling root-cause analysis and prompt optimization."
tags: [observability, debugging, counterfactual, replay, analysis, evaluation]
difficulty: advanced
solution_count: 6
---

## Problem

When an agent makes a bad decision — wrong tool choice, hallucination, incorrect routing — debugging is guesswork. You can see what the agent did, but not why, and not what a small change would have produced. Without counterfactual analysis, improvements are trial-and-error: change the prompt, redeploy, and hope it's better.

```python
# Bad: no ability to ask "what if?"
response = await agent.run(user_message)
# Agent made a wrong tool call. Why? What if the system prompt were different?
# What if the temperature were 0? No way to know without rerunning from scratch.
```

---

## Solution 1 — Decision Point Logger with Replay Interface

Log every decision point (LLM call inputs + outputs) in a replayable format. Later, swap any field and re-run to see what would have changed.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class DecisionPoint:
    decision_id: str
    session_id: str
    ts: float
    model: str
    system: str
    messages: list[dict]
    temperature: float
    max_tokens: int
    response_text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

DECISION_LOG_PATH = Path("/var/log/agent/decisions.jsonl")

def log_decision(dp: DecisionPoint) -> None:
    with open(DECISION_LOG_PATH, "a") as f:
        f.write(json.dumps(dp.to_dict()) + "\n")

async def instrumented_llm_call(
    session_id: str,
    system: str,
    messages: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    temperature: float = 1.0,
    max_tokens: int = 512,
    metadata: dict = None,
) -> tuple[str, str]:
    """LLM call that logs the full decision point for later replay."""
    response = await client.messages.create(
        model=model,
        system=system,
        max_tokens=max_tokens,
        messages=messages,
    )
    output = response.content[0].text
    dp = DecisionPoint(
        decision_id=str(uuid.uuid4()),
        session_id=session_id,
        ts=time.time(),
        model=model,
        system=system,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_text=output,
        stop_reason=response.stop_reason,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        metadata=metadata or {},
    )
    log_decision(dp)
    return output, dp.decision_id

def load_decision(decision_id: str) -> DecisionPoint | None:
    with open(DECISION_LOG_PATH) as f:
        for line in f:
            dp = json.loads(line)
            if dp["decision_id"] == decision_id:
                return DecisionPoint(**dp)
    return None

async def replay_with_changes(decision_id: str, **overrides) -> dict:
    """Replay a logged decision with modified fields. Returns comparison."""
    original = load_decision(decision_id)
    if not original:
        raise ValueError(f"Decision {decision_id} not found")

    # Apply overrides
    replay_args = {
        "model": overrides.get("model", original.model),
        "system": overrides.get("system", original.system),
        "max_tokens": overrides.get("max_tokens", original.max_tokens),
        "messages": overrides.get("messages", original.messages),
    }
    response = await client.messages.create(**replay_args)
    counterfactual = response.content[0].text

    return {
        "original": original.response_text,
        "counterfactual": counterfactual,
        "changed_fields": list(overrides.keys()),
        "original_tokens": original.output_tokens,
        "counterfactual_tokens": response.usage.output_tokens,
    }

# Usage
async def demo():
    session_id = str(uuid.uuid4())
    output, decision_id = await instrumented_llm_call(
        session_id=session_id,
        system="You are a helpful assistant. Always answer in bullet points.",
        messages=[{"role": "user", "content": "What is quantum computing?"}],
    )
    print(f"Original: {output[:100]}")

    # Counterfactual: what if we hadn't required bullet points?
    comparison = await replay_with_changes(
        decision_id,
        system="You are a helpful assistant.",
    )
    print(f"Without bullet point instruction: {comparison['counterfactual'][:100]}")
    print(f"Changed: {comparison['changed_fields']}")

asyncio.run(demo())
```

---

## Solution 2 — Tool Choice Counterfactual: What If a Different Tool Were Called?

For tool-use decisions, replay the conversation with the actual tool result replaced by a hypothetical result, then continue execution to see how the final answer changes.

```python
import asyncio
import copy
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def run_agent_with_tool_override(
    messages: list[dict],
    system: str,
    tool_name_to_override: str,
    counterfactual_result: str,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    """
    Run agent normally until it calls `tool_name_to_override`,
    then inject the counterfactual result instead of the real one.
    """
    tools = [
        {
            "name": "web_search",
            "description": "Search the web",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                            "required": ["query"]},
        },
        {
            "name": "calculator",
            "description": "Evaluate math expressions",
            "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}},
                            "required": ["expr"]},
        },
    ]

    current_messages = copy.deepcopy(messages)
    intercepted = False

    for _ in range(5):  # max turns
        response = await client.messages.create(
            model=model,
            system=system,
            max_tokens=512,
            tools=tools,
            messages=current_messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            current_messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == tool_name_to_override and not intercepted:
                    # Inject counterfactual result
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": counterfactual_result,
                    })
                    intercepted = True
                    print(f"Intercepted {block.name} call — injecting counterfactual")
                else:
                    # Real tool execution
                    real_result = f"real result for {block.name}({block.input})"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": real_result,
                    })
            current_messages.append({"role": "user", "content": tool_results})

    return "Max turns reached"

async def compare_tool_outcomes(
    messages: list[dict],
    system: str,
    tool_name: str,
    result_a: str,
    result_b: str,
) -> dict:
    """Run agent with two different tool results, compare final answers."""
    answer_a, answer_b = await asyncio.gather(
        run_agent_with_tool_override(messages, system, tool_name, result_a),
        run_agent_with_tool_override(messages, system, tool_name, result_b),
    )
    return {
        "tool": tool_name,
        "result_a": result_a[:100],
        "answer_a": answer_a[:200],
        "result_b": result_b[:100],
        "answer_b": answer_b[:200],
        "diverged": answer_a != answer_b,
    }

async def demo():
    messages = [{"role": "user", "content": "What is the current Bitcoin price?"}]
    system = "Use tools to answer accurately."

    comparison = await compare_tool_outcomes(
        messages, system,
        tool_name="web_search",
        result_a="Bitcoin is currently trading at $45,000 USD",
        result_b="Bitcoin is currently trading at $95,000 USD",
    )
    print(f"Diverged: {comparison['diverged']}")
    print(f"A: {comparison['answer_a'][:100]}")
    print(f"B: {comparison['answer_b'][:100]}")

asyncio.run(demo())
```

---

## Solution 3 — LLM-as-Judge Counterfactual Evaluator

Use a judge model to compare the original decision against counterfactuals along specific quality dimensions (accuracy, helpfulness, safety).

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

JUDGE_PROMPT = """\
You are an expert evaluator comparing two AI agent responses to the same user query.

User query: {query}

Response A (original): {response_a}
Response B (alternative): {response_b}

Evaluate on these dimensions (score each 1-10, then give a winner):
1. Accuracy: factual correctness
2. Helpfulness: how well it addresses the user's need
3. Conciseness: appropriate length without padding
4. Safety: avoids harmful content

Return JSON:
{{
  "accuracy": {{"a": 1-10, "b": 1-10}},
  "helpfulness": {{"a": 1-10, "b": 1-10}},
  "conciseness": {{"a": 1-10, "b": 1-10}},
  "safety": {{"a": 1-10, "b": 1-10}},
  "overall_winner": "A" | "B" | "tie",
  "explanation": "one sentence explaining the key difference"
}}"""

async def judge_counterfactual(
    query: str,
    original_response: str,
    counterfactual_response: str,
) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": JUDGE_PROMPT.format(
                query=query,
                response_a=original_response,
                response_b=counterfactual_response,
            )
        }],
    )
    text = response.content[0].text.strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"raw": text, "parse_error": True}

async def multi_counterfactual_analysis(
    query: str,
    original: str,
    alternatives: dict[str, str],  # name -> response
) -> dict:
    """Judge original against multiple alternatives simultaneously."""
    judgments = await asyncio.gather(*[
        judge_counterfactual(query, original, alt)
        for alt in alternatives.values()
    ])
    results = {}
    for name, judgment in zip(alternatives.keys(), judgments):
        score_orig = sum(judgment.get(d, {}).get("a", 5)
                        for d in ["accuracy", "helpfulness", "conciseness", "safety"])
        score_alt = sum(judgment.get(d, {}).get("b", 5)
                       for d in ["accuracy", "helpfulness", "conciseness", "safety"])
        results[name] = {
            "original_score": score_orig,
            "alternative_score": score_alt,
            "winner": judgment.get("overall_winner"),
            "explanation": judgment.get("explanation", ""),
        }

    best = max(results.items(), key=lambda x: x[1]["alternative_score"])
    return {"alternatives": results, "best_alternative": best[0]}

async def demo():
    query = "Explain recursion in programming"
    original = "Recursion is when a function calls itself."

    alternatives = {
        "with_example": "Recursion is when a function calls itself. For example: def factorial(n): return 1 if n<=1 else n*factorial(n-1)",
        "eli5": "Imagine looking in a mirror while holding another mirror — you see infinite reflections. Recursion is a function that keeps calling itself until it hits a stopping condition.",
        "technical": "Recursion is a programming technique where a function invokes itself with modified arguments, building a call stack until a base case terminates the chain.",
    }

    result = await multi_counterfactual_analysis(query, original, alternatives)
    print(f"Best alternative: {result['best_alternative']}")
    for name, scores in result["alternatives"].items():
        print(f"  {name}: orig={scores['original_score']} alt={scores['alternative_score']} winner={scores['winner']}")

asyncio.run(demo())
```

---

## Solution 4 — Prompt Sensitivity Analysis: Vary One Factor at a Time

Systematically vary individual prompt components (temperature, system prompt sections, few-shot examples) while holding others constant — a controlled experiment framework.

```python
import asyncio
import json
from dataclasses import dataclass
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class PromptFactor:
    name: str
    values: list[str | float | int]

@dataclass
class SensitivityResult:
    factor: str
    value: str
    response: str
    tokens: int

async def run_factor(query: str, base_system: str,
                     factor: PromptFactor, value) -> SensitivityResult:
    """Run one experiment varying a single factor."""
    if factor.name == "system_prompt":
        system = value
        temp_kwargs = {}
    elif factor.name == "system_prefix":
        system = f"{value}\n\n{base_system}"
        temp_kwargs = {}
    elif factor.name == "max_tokens":
        system = base_system
        temp_kwargs = {"max_tokens": int(value)}
    else:
        system = base_system
        temp_kwargs = {}

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=temp_kwargs.get("max_tokens", 256),
        system=system,
        messages=[{"role": "user", "content": query}],
    )
    return SensitivityResult(
        factor=factor.name,
        value=str(value)[:50],
        response=response.content[0].text,
        tokens=response.usage.output_tokens,
    )

async def sensitivity_analysis(
    query: str,
    base_system: str,
    factors: list[PromptFactor],
    max_concurrent: int = 5,
) -> dict[str, list[SensitivityResult]]:
    """Run all factor variations concurrently, grouped by factor."""
    semaphore = asyncio.Semaphore(max_concurrent)

    async def bounded_run(factor, value):
        async with semaphore:
            return await run_factor(query, base_system, factor, value)

    results: dict[str, list[SensitivityResult]] = {}
    for factor in factors:
        factor_results = await asyncio.gather(
            *[bounded_run(factor, v) for v in factor.values]
        )
        results[factor.name] = list(factor_results)

    return results

def summarize_sensitivity(results: dict[str, list[SensitivityResult]]) -> None:
    for factor_name, factor_results in results.items():
        print(f"\n=== Factor: {factor_name} ===")
        for r in factor_results:
            print(f"  [{r.value}] ({r.tokens} tokens)")
            print(f"    {r.response[:100]!r}")

async def demo():
    query = "What should I do when I feel overwhelmed?"
    base_system = "You are a helpful assistant."

    factors = [
        PromptFactor("system_prefix", [
            "Be concise. Limit to 50 words.",
            "Be thorough and empathetic.",
            "Reply in bullet points only.",
        ]),
        PromptFactor("max_tokens", [50, 150, 400]),
    ]

    results = await sensitivity_analysis(query, base_system, factors)
    summarize_sensitivity(results)

asyncio.run(demo())
```

---

## Solution 5 — Causal Tracing: Which Context Tokens Changed the Decision?

Use attention-inspired probing to identify which parts of the input most influenced the output, by systematically ablating sections and measuring output divergence.

```python
import asyncio
import difflib
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def cosine_similarity_text(a: str, b: str) -> float:
    """Simple token-overlap similarity (0-1). Replace with embeddings in production."""
    a_tokens = set(a.lower().split())
    b_tokens = set(b.lower().split())
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = len(a_tokens & b_tokens)
    return intersection / max(len(a_tokens), len(b_tokens))

async def measure_ablation_impact(
    query: str,
    full_system: str,
    ablated_system: str,
    model: str = "claude-haiku-4-5-20251001",
) -> float:
    """Measure how much removing a section changes the output (0=no change, 1=completely different)."""
    full_response, ablated_response = await asyncio.gather(
        client.messages.create(model=model, max_tokens=256, system=full_system,
                               messages=[{"role": "user", "content": query}]),
        client.messages.create(model=model, max_tokens=256, system=ablated_system,
                               messages=[{"role": "user", "content": query}]),
    )
    full_text = full_response.content[0].text
    ablated_text = ablated_response.content[0].text
    similarity = cosine_similarity_text(full_text, ablated_text)
    return 1.0 - similarity  # divergence

async def causal_trace(
    query: str,
    system: str,
    sections: dict[str, str],  # name -> text to ablate
) -> dict[str, float]:
    """Return impact score for each section (higher = more influential)."""
    semaphore = asyncio.Semaphore(3)

    async def measure_one(name: str, text: str) -> tuple[str, float]:
        async with semaphore:
            ablated = system.replace(text, "").strip()
            impact = await measure_ablation_impact(query, system, ablated)
            return name, impact

    results = await asyncio.gather(*[measure_one(n, t) for n, t in sections.items()])
    impacts = dict(results)
    # Sort by impact
    return dict(sorted(impacts.items(), key=lambda x: -x[1]))

async def demo():
    system = """You are a customer service agent for TechCorp.
Be polite and professional at all times.
Never discuss competitor products.
Always offer a follow-up question to keep the conversation going.
If the user is frustrated, acknowledge their feelings first."""

    sections = {
        "politeness_rule": "Be polite and professional at all times.",
        "competitor_rule": "Never discuss competitor products.",
        "follow_up_rule": "Always offer a follow-up question to keep the conversation going.",
        "empathy_rule": "If the user is frustrated, acknowledge their feelings first.",
    }

    query = "Your product broke after 2 days and your support is terrible!"
    impacts = await causal_trace(query, system, sections)

    print("Causal trace — most influential rules:")
    for rule, impact in impacts.items():
        bar = "█" * int(impact * 20)
        print(f"  {rule:25s} {bar} {impact:.2f}")

asyncio.run(demo())
```

---

## Solution 6 — A/B Counterfactual Registry: Track Improvements Over Time

Systematically record original-vs-counterfactual pairs, score them, and maintain a registry that tracks which prompt changes produced measurable improvements.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class CounterfactualRecord:
    record_id: str
    ts: float
    query: str
    original_system: str
    original_response: str
    counterfactual_system: str
    counterfactual_response: str
    change_description: str
    judge_score_original: float = 0.0
    judge_score_counterfactual: float = 0.0
    improvement: float = 0.0  # positive = counterfactual is better
    tags: list[str] = field(default_factory=list)

REGISTRY_PATH = Path("/var/log/agent/counterfactuals.jsonl")

async def run_and_record_counterfactual(
    query: str,
    original_system: str,
    counterfactual_system: str,
    change_description: str,
    tags: list[str] = None,
) -> CounterfactualRecord:
    """Run both variants, judge them, and persist the comparison."""
    # Run both in parallel
    orig_resp, cf_resp = await asyncio.gather(
        client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=256,
                               system=original_system,
                               messages=[{"role": "user", "content": query}]),
        client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=256,
                               system=counterfactual_system,
                               messages=[{"role": "user", "content": query}]),
    )

    # Judge both
    judge_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": (
            f"Rate these two responses to '{query[:100]}' on helpfulness 1-10.\n"
            f"A: {orig_resp.content[0].text[:200]}\n"
            f"B: {cf_resp.content[0].text[:200]}\n"
            f"Return JSON: {{\"a\": score, \"b\": score}}"
        )}],
    )
    try:
        text = judge_resp.content[0].text
        scores = json.loads(text[text.find("{"):text.rfind("}")+1])
        score_a = float(scores.get("a", 5))
        score_b = float(scores.get("b", 5))
    except Exception:
        score_a = score_b = 5.0

    record = CounterfactualRecord(
        record_id=str(uuid.uuid4()),
        ts=time.time(),
        query=query,
        original_system=original_system,
        original_response=orig_resp.content[0].text,
        counterfactual_system=counterfactual_system,
        counterfactual_response=cf_resp.content[0].text,
        change_description=change_description,
        judge_score_original=score_a,
        judge_score_counterfactual=score_b,
        improvement=score_b - score_a,
        tags=tags or [],
    )

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "a") as f:
        f.write(json.dumps(asdict(record)) + "\n")

    return record

def analyze_registry() -> dict:
    """Summarize which changes improved / degraded quality."""
    records = []
    with open(REGISTRY_PATH) as f:
        for line in f:
            records.append(json.loads(line))

    improvements = [r for r in records if r["improvement"] > 0.5]
    regressions = [r for r in records if r["improvement"] < -0.5]
    return {
        "total_experiments": len(records),
        "improvements": len(improvements),
        "regressions": len(regressions),
        "top_improvements": sorted(improvements, key=lambda r: -r["improvement"])[:3],
        "worst_regressions": sorted(regressions, key=lambda r: r["improvement"])[:3],
        "avg_improvement": sum(r["improvement"] for r in records) / max(len(records), 1),
    }
```

---

## Comparison

| Approach | Requires Rerun | Automated | Isolates Cause | Tracks History | Best For |
|---|---|---|---|---|---|
| Decision point replay | **Yes** | Partial | Partial | No | Ad-hoc debugging |
| Tool result injection | **Yes** | No | **Yes** (tool-level) | No | Tool call analysis |
| LLM-as-judge comparison | **Yes** | **Yes** | Partial | No | Quality comparison |
| Sensitivity analysis | **Yes** | **Yes** | **Yes** (factor-level) | No | Prompt component testing |
| Causal ablation tracing | **Yes** | **Yes** | **Yes** (rule-level) | No | Rule importance ranking |
| A/B counterfactual registry | **Yes** | **Yes** | Partial | **Yes** | Systematic prompt improvement |
