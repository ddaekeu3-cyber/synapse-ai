---
title: "Agent Doesn't Implement Prompt Token Efficiency Benchmarking"
description: "Agent prompts consume tokens without measurement; engineers cannot tell whether a refactored system prompt saves tokens, whether verbosity improves quality, or which prompt version delivers the best output-per-token ratio."
category: observability
difficulty: intermediate
tags: [tokens, efficiency, benchmarking, prompt-engineering, cost, quality, measurement]
---

# Agent Doesn't Implement Prompt Token Efficiency Benchmarking

## Problem

Token cost is a direct function of prompt length: a 2× shorter system prompt cuts input costs by 2×. But shorter prompts sometimes reduce output quality. Without benchmarking, engineers make prompt changes based on intuition — they don't know if a 200-token reduction in system prompt length causes a measurable quality drop or has zero effect. Token efficiency benchmarking quantifies the tradeoff: tokens spent vs. quality delivered, across prompt versions.

## Solution 1: Token Usage Recorder — Capture Input/Output Tokens Per Turn

Record input tokens, output tokens, and a quality signal for every agent turn to build a baseline for efficiency analysis.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class TurnRecord:
    turn_id: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    prompt_version: str
    quality_score: float | None = None  # set externally (human rating, LLM judge, etc.)
    timestamp: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_per_output_token(self) -> float:
        """How many total tokens were consumed per output token produced."""
        return self.total_tokens / self.output_tokens if self.output_tokens else 0

class TokenEfficiencyRecorder:
    def __init__(self):
        self._records: list[TurnRecord] = []

    async def call(
        self,
        system: str,
        messages: list[dict],
        model: str = "claude-haiku-4-5-20251001",
        prompt_version: str = "v1",
    ) -> tuple[str, TurnRecord]:
        import uuid
        start = time.monotonic()
        resp = await client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=messages,
        )
        latency_ms = (time.monotonic() - start) * 1000

        record = TurnRecord(
            turn_id=str(uuid.uuid4())[:8],
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            latency_ms=round(latency_ms, 1),
            prompt_version=prompt_version,
        )
        self._records.append(record)
        return resp.content[0].text, record

    def summary_by_version(self) -> dict[str, dict]:
        from collections import defaultdict
        buckets: dict[str, list[TurnRecord]] = defaultdict(list)
        for r in self._records:
            buckets[r.prompt_version].append(r)

        result = {}
        for version, records in buckets.items():
            n = len(records)
            result[version] = {
                "samples": n,
                "avg_input_tokens": round(sum(r.input_tokens for r in records) / n, 1),
                "avg_output_tokens": round(sum(r.output_tokens for r in records) / n, 1),
                "avg_total_tokens": round(sum(r.total_tokens for r in records) / n, 1),
                "avg_latency_ms": round(sum(r.latency_ms for r in records) / n, 1),
                "avg_quality": round(
                    sum(r.quality_score for r in records if r.quality_score is not None) /
                    max(1, sum(1 for r in records if r.quality_score is not None)), 3
                ),
            }
        return result

recorder = TokenEfficiencyRecorder()

SYSTEM_V1 = """You are a helpful customer support agent for Acme Corp.
You assist customers with their questions about our products and services.
Always be polite, professional, and concise in your responses.
If you don't know the answer, say so and offer to escalate."""

SYSTEM_V2 = "Acme support agent. Polite, concise. Escalate unknowns."

async def benchmark_prompt_versions(test_messages: list[str]) -> dict:
    for msg in test_messages:
        await recorder.call(SYSTEM_V1, [{"role": "user", "content": msg}], prompt_version="v1_verbose")
        await recorder.call(SYSTEM_V2, [{"role": "user", "content": msg}], prompt_version="v2_concise")

    return recorder.summary_by_version()
```

**When to use**: Baseline for any prompt optimization project. You cannot improve what you don't measure.

---

## Solution 2: Prompt Version A/B Test — Measure Quality Tradeoff

Run both prompt versions on the same inputs; use LLM-as-judge to score output quality. Report tokens saved vs. quality delta.

```python
import asyncio
from dataclasses import dataclass
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

JUDGE_SYSTEM = """You are an expert evaluator of AI assistant responses.
Rate responses on a scale of 1-5 where:
5 = Perfect: accurate, helpful, concise
4 = Good: accurate and helpful, minor issues
3 = Adequate: mostly correct, some gaps
2 = Poor: significant accuracy or helpfulness issues
1 = Unacceptable: wrong or unhelpful
Respond with ONLY the number."""

@dataclass
class ABResult:
    prompt_version: str
    input_tokens: int
    output_tokens: int
    response: str
    quality_score: float

async def judge_response(question: str, response: str) -> float:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        system=JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nResponse to rate:\n{response}",
        }],
    )
    try:
        return float(resp.content[0].text.strip())
    except ValueError:
        return 3.0

async def ab_test_prompt(
    question: str,
    prompt_a: tuple[str, str],  # (name, system_prompt)
    prompt_b: tuple[str, str],
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    async def run_version(name: str, system: str) -> ABResult:
        resp = await client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        response_text = resp.content[0].text
        quality = await judge_response(question, response_text)
        return ABResult(
            prompt_version=name,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            response=response_text,
            quality_score=quality,
        )

    result_a, result_b = await asyncio.gather(
        run_version(*prompt_a),
        run_version(*prompt_b),
    )

    token_savings = result_a.input_tokens - result_b.input_tokens
    quality_delta = result_b.quality_score - result_a.quality_score
    tokens_saved_pct = round(100 * token_savings / result_a.input_tokens, 1) if result_a.input_tokens else 0

    return {
        "question": question,
        "version_a": {
            "name": prompt_a[0],
            "input_tokens": result_a.input_tokens,
            "output_tokens": result_a.output_tokens,
            "quality": result_a.quality_score,
        },
        "version_b": {
            "name": prompt_b[0],
            "input_tokens": result_b.input_tokens,
            "output_tokens": result_b.output_tokens,
            "quality": result_b.quality_score,
        },
        "analysis": {
            "input_tokens_saved": token_savings,
            "tokens_saved_pct": tokens_saved_pct,
            "quality_delta": round(quality_delta, 2),
            "efficiency_improved": token_savings > 0 and quality_delta >= -0.5,
            "recommendation": (
                f"Version B saves {token_savings} input tokens ({tokens_saved_pct}%) "
                f"with quality change of {quality_delta:+.1f}/5"
            ),
        },
    }

async def multi_question_ab_test(
    questions: list[str],
    prompt_a: tuple[str, str],
    prompt_b: tuple[str, str],
) -> dict:
    results = await asyncio.gather(*[
        ab_test_prompt(q, prompt_a, prompt_b) for q in questions
    ])
    avg_token_savings = sum(r["analysis"]["input_tokens_saved"] for r in results) / len(results)
    avg_quality_delta = sum(r["analysis"]["quality_delta"] for r in results) / len(results)
    return {
        "questions_tested": len(questions),
        "avg_token_savings_per_call": round(avg_token_savings, 1),
        "avg_quality_delta": round(avg_quality_delta, 3),
        "results": results,
    }
```

**When to use**: Before deploying a prompt refactor. A/B testing with an LLM judge gives a defensible "tokens saved vs. quality impact" number that justifies (or blocks) the change.

---

## Solution 3: Prompt Component Profiler — Identify Which Sections Cost the Most

Break the system prompt into labeled sections and measure the token cost of each, so you know where to focus optimization effort.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def count_tokens_approx(text: str) -> int:
    """Rough token count: ~4 chars per token."""
    return max(1, len(text) // 4)

def profile_prompt_sections(
    system_prompt: str,
    section_labels: dict[str, str],  # label → text
) -> dict:
    """
    Measure the token cost of each section in the system prompt.
    Returns a breakdown of token usage by section.
    """
    total_tokens = count_tokens_approx(system_prompt)
    sections = []

    for label, text in section_labels.items():
        tokens = count_tokens_approx(text)
        sections.append({
            "label": label,
            "tokens": tokens,
            "pct_of_total": round(100 * tokens / total_tokens, 1),
            "chars": len(text),
        })

    sections.sort(key=lambda x: -x["tokens"])

    return {
        "total_tokens": total_tokens,
        "sections": sections,
        "top_consumer": sections[0]["label"] if sections else None,
        "optimization_targets": [s for s in sections if s["pct_of_total"] > 20],
    }

async def agent_prompt_profiler():
    # Example system prompt with labeled sections
    sections = {
        "persona":
            "You are a professional customer support specialist for Acme Corp, "
            "a leading provider of enterprise software solutions. "
            "You have extensive knowledge of our product line and company policies.",

        "behavior_rules":
            "BEHAVIOR GUIDELINES:\n"
            "- Always greet the customer warmly\n"
            "- Use formal language and avoid slang\n"
            "- Never promise what cannot be delivered\n"
            "- Always offer to follow up on open issues\n"
            "- End each conversation with a satisfaction check\n"
            "- Escalate to a human agent if the issue cannot be resolved in 3 turns\n"
            "- Do not discuss competitor products\n"
            "- Do not discuss internal company finances\n"
            "- Always use the customer's name if provided",

        "product_catalog":
            "PRODUCTS:\n"
            "- AcmeSuite Pro: Enterprise workflow automation ($499/mo)\n"
            "- AcmeSuite Lite: SMB workflow automation ($99/mo)\n"
            "- AcmeAnalytics: Business intelligence platform ($299/mo)\n"
            "- AcmeConnect: API integration hub ($199/mo)\n"
            "- AcmeVault: Document management ($149/mo)",

        "response_format":
            "FORMAT: Respond in 2-3 sentences. Be concise. Use plain language.",
    }

    full_prompt = "\n\n".join(sections.values())
    profile = profile_prompt_sections(full_prompt, sections)

    print("Prompt Token Profile:")
    print(f"  Total: {profile['total_tokens']} tokens")
    for s in profile["sections"]:
        print(f"  {s['label']}: {s['tokens']} tokens ({s['pct_of_total']}%)")

    print(f"\nTop cost section: {profile['top_consumer']}")
    print(f"Optimization targets (>20%): {[s['label'] for s in profile['optimization_targets']]}")

    return profile
```

**When to use**: Before starting a prompt optimization project. The profiler shows exactly which sections to focus on — no point optimizing a 30-token persona section when the 400-token behavior rules section dominates.

---

## Solution 4: Historical Efficiency Tracker — Chart Token Efficiency Over Prompt Versions

Track tokens-per-quality-point over time across prompt versions to see if prompt engineering is improving or degrading efficiency.

```python
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

HISTORY_FILE = Path("/tmp/prompt_efficiency_history.json")

@dataclass
class EfficiencySnapshot:
    prompt_version: str
    avg_input_tokens: float
    avg_output_tokens: float
    avg_quality_score: float
    samples: int
    recorded_at: float = field(default_factory=time.time)

    @property
    def tokens_per_quality_point(self) -> float:
        """Lower is better: fewer tokens consumed per unit of quality."""
        if self.avg_quality_score == 0:
            return float("inf")
        return (self.avg_input_tokens + self.avg_output_tokens) / self.avg_quality_score

def load_history() -> list[EfficiencySnapshot]:
    if not HISTORY_FILE.exists():
        return []
    data = json.loads(HISTORY_FILE.read_text())
    return [EfficiencySnapshot(**d) for d in data]

def save_snapshot(snapshot: EfficiencySnapshot) -> None:
    history = load_history()
    history.append(snapshot)
    HISTORY_FILE.write_text(json.dumps([asdict(s) for s in history], default=str))

def compute_efficiency_trend(history: list[EfficiencySnapshot]) -> list[dict]:
    """Compute efficiency trend across prompt versions."""
    if not history:
        return []

    baseline = history[0].tokens_per_quality_point
    trend = []
    for snap in history:
        tpq = snap.tokens_per_quality_point
        change_pct = round(100 * (tpq - baseline) / baseline, 1) if baseline else 0
        trend.append({
            "version": snap.prompt_version,
            "tokens_per_quality_point": round(tpq, 2),
            "change_vs_baseline_pct": change_pct,
            "improved": change_pct < 0,  # lower tokens/quality = better
            "recorded_at": snap.recorded_at,
        })
    return trend

async def evaluate_and_record_prompt(
    prompt_version: str,
    system_prompt: str,
    test_questions: list[str],
    quality_scores: list[float] | None = None,
) -> EfficiencySnapshot:
    """
    Run test questions against a prompt version; record efficiency snapshot.
    quality_scores can be provided externally (human ratings) or default to 3.5.
    """
    total_input = 0
    total_output = 0

    for question in test_questions:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        total_input += resp.usage.input_tokens
        total_output += resp.usage.output_tokens

    n = len(test_questions)
    snapshot = EfficiencySnapshot(
        prompt_version=prompt_version,
        avg_input_tokens=round(total_input / n, 1),
        avg_output_tokens=round(total_output / n, 1),
        avg_quality_score=(sum(quality_scores) / len(quality_scores)) if quality_scores else 3.5,
        samples=n,
    )
    save_snapshot(snapshot)

    history = load_history()
    trend = compute_efficiency_trend(history)
    latest = trend[-1] if trend else {}

    return snapshot

async def efficiency_report():
    history = load_history()
    trend = compute_efficiency_trend(history)
    print("Prompt Efficiency History:")
    for entry in trend:
        arrow = "↓" if entry["improved"] else "↑"
        print(f"  {entry['version']}: {entry['tokens_per_quality_point']:.1f} t/q {arrow} {entry['change_vs_baseline_pct']:+.1f}%")
    return trend
```

**When to use**: Teams with an active prompt engineering practice. Historical tracking turns "we've been improving the prompt for 6 months" from a feeling into a measurable trend with hard numbers.

---

## Solution 5: Cost-Quality Frontier — Find the Pareto Optimal Prompt

Evaluate multiple prompt variants on cost (tokens) and quality axes, then identify which variants are on the Pareto frontier (no other variant is both cheaper and better).

```python
import asyncio
from dataclasses import dataclass
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class PromptVariant:
    name: str
    system: str
    avg_tokens: float = 0.0
    avg_quality: float = 0.0

def pareto_frontier(variants: list[PromptVariant]) -> list[str]:
    """
    Find prompt variants that are NOT dominated.
    A variant is dominated if another variant has lower tokens AND higher quality.
    Returns names of Pareto-optimal variants.
    """
    pareto = []
    for v in variants:
        dominated = False
        for other in variants:
            if other.name == v.name:
                continue
            # other dominates v if other has ≤ tokens AND ≥ quality (strict on at least one)
            if other.avg_tokens <= v.avg_tokens and other.avg_quality >= v.avg_quality:
                if other.avg_tokens < v.avg_tokens or other.avg_quality > v.avg_quality:
                    dominated = True
                    break
        if not dominated:
            pareto.append(v.name)
    return pareto

async def evaluate_variant(
    variant: PromptVariant,
    test_questions: list[str],
) -> PromptVariant:
    """Evaluate a prompt variant and fill in avg_tokens + avg_quality."""
    total_input = 0
    qualities = []

    for question in test_questions:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=variant.system,
            messages=[{"role": "user", "content": question}],
        )
        total_input += resp.usage.input_tokens

        # LLM judge quality score
        judge = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4,
            system="Rate response quality 1-5 (5=best). Reply with the number only.",
            messages=[{"role": "user", "content": f"Q: {question}\nA: {resp.content[0].text}"}],
        )
        try:
            qualities.append(float(judge.content[0].text.strip()))
        except ValueError:
            qualities.append(3.0)

    variant.avg_tokens = total_input / len(test_questions)
    variant.avg_quality = sum(qualities) / len(qualities)
    return variant

async def find_pareto_optimal_prompt(
    variants: list[PromptVariant],
    test_questions: list[str],
) -> dict:
    evaluated = await asyncio.gather(*[
        evaluate_variant(v, test_questions) for v in variants
    ])

    optimal_names = pareto_frontier(list(evaluated))

    results = sorted(
        [{"name": v.name, "avg_tokens": round(v.avg_tokens, 1), "avg_quality": round(v.avg_quality, 2),
          "pareto_optimal": v.name in optimal_names}
         for v in evaluated],
        key=lambda x: x["avg_tokens"],
    )

    return {
        "variants_evaluated": len(evaluated),
        "pareto_optimal": optimal_names,
        "recommendation": f"Use one of: {optimal_names}",
        "results": results,
    }
```

**When to use**: When choosing between multiple candidate prompts. The Pareto frontier tells you which variants are worth considering — anything off the frontier is strictly worse than something else.

---

## Solution 6: Continuous Efficiency Monitor — Alert on Token Regression

Alert when a prompt change causes a significant increase in average input tokens without a corresponding quality improvement.

```python
import asyncio
import logging
from collections import deque
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
logger = logging.getLogger("prompt_efficiency")

class ContinuousEfficiencyMonitor:
    """
    Tracks rolling average tokens and quality.
    Alerts when efficiency regresses beyond a threshold.
    """

    def __init__(
        self,
        window_size: int = 100,
        token_regression_threshold: float = 0.15,  # 15% more tokens = alert
        quality_drop_threshold: float = 0.3,        # 0.3/5 quality drop = alert
    ):
        self._window = window_size
        self._token_threshold = token_regression_threshold
        self._quality_threshold = quality_drop_threshold
        self._token_history: deque[float] = deque(maxlen=window_size)
        self._quality_history: deque[float] = deque(maxlen=window_size)
        self._baseline_tokens: float | None = None
        self._baseline_quality: float | None = None

    def record(self, input_tokens: int, quality_score: float) -> dict | None:
        self._token_history.append(float(input_tokens))
        self._quality_history.append(quality_score)

        if len(self._token_history) < 10:
            return None  # not enough data

        avg_tokens = sum(self._token_history) / len(self._token_history)
        avg_quality = sum(self._quality_history) / len(self._quality_history)

        if self._baseline_tokens is None:
            self._baseline_tokens = avg_tokens
            self._baseline_quality = avg_quality
            return None

        token_change = (avg_tokens - self._baseline_tokens) / self._baseline_tokens
        quality_change = avg_quality - (self._baseline_quality or 0)

        alert = None
        if token_change > self._token_threshold and quality_change <= 0:
            alert = {
                "type": "token_regression",
                "avg_tokens": round(avg_tokens, 1),
                "baseline_tokens": round(self._baseline_tokens, 1),
                "token_increase_pct": round(token_change * 100, 1),
                "quality_change": round(quality_change, 3),
                "recommendation": "Review recent prompt changes — token cost increased without quality gain",
            }
            logger.warning("token_efficiency_regression", extra=alert)

        if quality_change < -self._quality_threshold:
            alert = {
                "type": "quality_regression",
                "avg_quality": round(avg_quality, 3),
                "baseline_quality": round(self._baseline_quality or 0, 3),
                "quality_drop": round(-quality_change, 3),
                "recommendation": "Quality dropped significantly — check recent prompt changes",
            }
            logger.warning("quality_regression", extra=alert)

        return alert

monitor = ContinuousEfficiencyMonitor(
    window_size=100,
    token_regression_threshold=0.15,
    quality_drop_threshold=0.3,
)

async def monitored_agent_call(user_message: str, system: str, quality_score: float | None = None) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    alert = monitor.record(
        input_tokens=resp.usage.input_tokens,
        quality_score=quality_score or 3.5,
    )
    result = {"response": resp.content[0].text, "input_tokens": resp.usage.input_tokens}
    if alert:
        result["efficiency_alert"] = alert
    return result
```

**When to use**: Production agents that undergo frequent prompt updates. Continuous monitoring catches efficiency regressions automatically — no need to remember to benchmark after every prompt change.

---

## Comparison

| Solution | Granularity | Automation | Historical | A/B | Pareto | Best For |
|---|---|---|---|---|---|---|
| Token usage recorder | Per-turn | Low | Yes | No | No | Baseline measurement |
| A/B test with LLM judge | Per-version | Medium | No | Yes | No | Pre-deployment validation |
| Prompt component profiler | Per-section | Low | No | No | No | Identifying optimization targets |
| Historical tracker | Per-version | Medium | Yes | No | No | Long-term trend analysis |
| Cost-quality frontier | Per-variant | High | No | Yes | Yes | Choosing among many variants |
| Continuous monitor | Per-turn | High | Yes | No | No | Production regression detection |

**Rule of thumb**: Start with the token usage recorder (Solution 1) to build a baseline. Run an A/B test with LLM-as-judge (Solution 2) before any prompt refactor. Add the continuous monitor (Solution 6) in production so regressions are caught automatically.
