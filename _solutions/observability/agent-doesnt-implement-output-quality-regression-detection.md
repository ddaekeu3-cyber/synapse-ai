---
title: "Agent Doesn't Implement Output Quality Regression Detection"
description: "Automatically detect when agent response quality degrades after model updates, prompt changes, or configuration shifts—catching regressions before users notice and before they affect business metrics."
difficulty: advanced
category: observability
tags: [observability, quality, regression-detection, monitoring, evaluation]
---

## Problem

Agent output quality silently degrades after deployments: a new model version changes tone, a prompt tweak breaks edge cases, or a configuration change causes formatting issues. Without automated quality regression detection, these problems are discovered through user complaints, support tickets, or manual reviews—days or weeks after the regression was introduced. Automated detection catches regressions within minutes of deployment.

## Solutions

### Option 1: Reference Comparison with Semantic Similarity

Compare new responses to a golden reference set and alert when similarity scores drop.

```python
import asyncio
import json
import os
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class ReferenceCase:
    prompt: str
    golden_response: str
    min_similarity: float = 0.7  # Minimum acceptable similarity

@dataclass
class QualityCheckResult:
    prompt: str
    new_response: str
    similarity_score: float
    passed: bool
    regression_detected: bool

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x ** 2 for x in a) ** 0.5
    mag_b = sum(x ** 2 for x in b) ** 0.5
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

async def get_embedding_score(text1: str, text2: str) -> float:
    """
    Approximate similarity using LLM-as-judge instead of embeddings.
    Replace with actual embedding API for production use.
    """
    judge_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system="Rate the semantic similarity of two texts from 0.0 to 1.0. Return ONLY the number.",
        messages=[{"role": "user", "content": f"Text 1: {text1[:200]}\nText 2: {text2[:200]}"}]
    )
    try:
        return float(judge_response.content[0].text.strip())
    except ValueError:
        return 0.5

GOLDEN_CASES = [
    ReferenceCase(
        prompt="What is asyncio in Python?",
        golden_response="asyncio is Python's built-in library for writing concurrent code using async/await syntax. It allows running multiple I/O-bound tasks cooperatively without threads.",
        min_similarity=0.65,
    ),
    ReferenceCase(
        prompt="How do I handle errors in async Python?",
        golden_response="Use try/except blocks inside async functions. For task groups, handle exceptions from asyncio.gather() by setting return_exceptions=True, or use asyncio.TaskGroup for structured error propagation.",
        min_similarity=0.60,
    ),
]

async def run_quality_check(
    cases: list[ReferenceCase],
    model: str = "claude-haiku-4-5-20251001",
) -> list[QualityCheckResult]:
    results = []

    for case in cases:
        response = await client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": case.prompt}]
        )
        new_response = response.content[0].text

        similarity = await get_embedding_score(case.golden_response, new_response)
        passed = similarity >= case.min_similarity

        results.append(QualityCheckResult(
            prompt=case.prompt,
            new_response=new_response,
            similarity_score=similarity,
            passed=passed,
            regression_detected=not passed,
        ))

    return results

def quality_report(results: list[QualityCheckResult]) -> dict:
    passed = sum(1 for r in results if r.passed)
    regressions = [r for r in results if r.regression_detected]

    report = {
        "total_cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": passed / len(results) if results else 0,
        "regressions": [
            {
                "prompt": r.prompt[:60],
                "similarity": f"{r.similarity_score:.2f}",
            }
            for r in regressions
        ],
    }
    return report

async def demo_reference_comparison():
    print("Running quality regression check...")
    results = await run_quality_check(GOLDEN_CASES)

    for r in results:
        status = "✓" if r.passed else "✗ REGRESSION"
        print(f"\n[{status}] {r.prompt[:50]}")
        print(f"  Similarity: {r.similarity_score:.2f}")
        print(f"  Response: {r.new_response.strip()[:100]}...")

    report = quality_report(results)
    print(f"\nQuality Report: {report['passed']}/{report['total_cases']} passed "
          f"({report['pass_rate']:.0%})")

asyncio.run(demo_reference_comparison())
```

### Option 2: LLM-as-Judge Regression Detection

Use a judge model to evaluate response quality on multiple dimensions and track scores over time.

```python
import asyncio
import json
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from pathlib import Path

client = AsyncAnthropic()
QUALITY_LOG = Path(".quality_scores.jsonl")

@dataclass
class QualityDimension:
    name: str
    weight: float
    description: str

DIMENSIONS = [
    QualityDimension("accuracy", 0.35, "Factual correctness"),
    QualityDimension("completeness", 0.25, "Addresses all parts of the question"),
    QualityDimension("clarity", 0.20, "Clear and well-structured"),
    QualityDimension("conciseness", 0.20, "Appropriately concise without padding"),
]

JUDGE_SYSTEM = f"""You are a quality evaluator for AI responses. Score each dimension 1-10.

Dimensions:
{chr(10).join(f'- {d.name}: {d.description}' for d in DIMENSIONS)}

Return JSON only:
{{"accuracy": N, "completeness": N, "clarity": N, "conciseness": N, "overall_comment": "..."}}"""

async def evaluate_response(prompt: str, response: str) -> dict:
    judge_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": f"Prompt: {prompt}\nResponse: {response}"}]
    )
    try:
        scores = json.loads(judge_response.content[0].text)
    except json.JSONDecodeError:
        scores = {d.name: 5 for d in DIMENSIONS}

    weighted_score = sum(
        scores.get(d.name, 5) * d.weight for d in DIMENSIONS
    )
    scores["weighted_score"] = weighted_score
    return scores

def log_quality_score(model: str, prompt: str, scores: dict):
    record = {
        "timestamp": time.time(),
        "model": model,
        "prompt_hash": hash(prompt) % 10000,
        "scores": scores,
    }
    with open(QUALITY_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")

def detect_regression(current_scores: dict, baseline_threshold: float = 6.0) -> list[str]:
    regressions = []
    for dim in DIMENSIONS:
        score = current_scores.get(dim.name, 5)
        if score < baseline_threshold:
            regressions.append(f"{dim.name}={score:.1f} (below threshold {baseline_threshold})")
    return regressions

async def monitored_completion(prompt: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    response = await client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text

    scores = await evaluate_response(prompt, text)
    log_quality_score(model, prompt, scores)
    regressions = detect_regression(scores)

    return {
        "response": text,
        "scores": scores,
        "regressions": regressions,
        "quality_ok": len(regressions) == 0,
    }

async def demo_llm_judge():
    test_prompts = [
        "Explain the difference between processes and threads.",
        "What is a context manager in Python?",
        "How does garbage collection work in Python?",
    ]

    for prompt in test_prompts:
        result = await monitored_completion(prompt)
        status = "✓" if result["quality_ok"] else "⚠ REGRESSION"
        print(f"\n[{status}] {prompt[:55]}")
        print(f"  Weighted score: {result['scores']['weighted_score']:.2f}/10")
        if result["regressions"]:
            print(f"  Regressions: {result['regressions']}")

asyncio.run(demo_llm_judge())
```

### Option 3: Statistical Process Control for Quality Scores

Apply control chart logic to quality scores—flag when scores fall outside statistical control limits.

```python
import asyncio
import json
import math
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from pathlib import Path

client = AsyncAnthropic()

@dataclass
class ControlChart:
    """Shewhart X-bar control chart for quality monitoring."""
    window_size: int = 20
    sigma_multiplier: float = 3.0  # 3-sigma control limits
    scores: list[float] = field(default_factory=list)

    def add(self, score: float) -> dict:
        self.scores.append(score)

        if len(self.scores) < 5:
            return {"status": "warming_up", "score": score}

        # Use last window_size scores to compute control limits
        recent = self.scores[-self.window_size:]
        mean = sum(recent) / len(recent)
        std = math.sqrt(sum((x - mean) ** 2 for x in recent) / len(recent))

        ucl = mean + self.sigma_multiplier * std  # Upper control limit
        lcl = max(0, mean - self.sigma_multiplier * std)  # Lower (floor at 0)

        out_of_control = score < lcl

        return {
            "score": score,
            "mean": mean,
            "std": std,
            "ucl": ucl,
            "lcl": lcl,
            "out_of_control": out_of_control,
            "status": "regression" if out_of_control else "in_control",
        }

async def quick_quality_score(prompt: str, response: str) -> float:
    """Score 0-10 using a lightweight judge."""
    judge = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        system="Rate this response quality 1-10. Return ONLY the number.",
        messages=[{"role": "user", "content": f"Prompt: {prompt}\nResponse: {response[:200]}"}]
    )
    try:
        return float(judge.content[0].text.strip())
    except ValueError:
        return 5.0

class SQCMonitor:
    def __init__(self):
        self._charts: dict[str, ControlChart] = {}
        self._alerts: list[dict] = []

    def get_chart(self, category: str) -> ControlChart:
        if category not in self._charts:
            self._charts[category] = ControlChart()
        return self._charts[category]

    async def record(self, prompt: str, response: str, category: str = "default") -> dict:
        score = await quick_quality_score(prompt, response)
        chart = self.get_chart(category)
        control_result = chart.add(score)

        if control_result.get("out_of_control"):
            alert = {
                "timestamp": time.time(),
                "category": category,
                "score": score,
                "mean": control_result["mean"],
                "lcl": control_result["lcl"],
                "prompt_preview": prompt[:60],
            }
            self._alerts.append(alert)
            print(f"⚠ SQC ALERT [{category}]: score={score:.1f} below LCL={control_result['lcl']:.1f}")

        return {**control_result, "category": category}

    def summary(self) -> dict:
        return {
            "total_alerts": len(self._alerts),
            "categories_monitored": list(self._charts.keys()),
            "recent_alerts": self._alerts[-3:],
        }

async def demo_sqc_monitoring():
    monitor = SQCMonitor()

    # Simulate a series of responses, with a quality dip in the middle
    prompts_responses = [
        ("What is a function?", "A function is a reusable block of code that performs a specific task."),
        ("What is a class?", "A class is a blueprint for creating objects with shared attributes and methods."),
        ("What is inheritance?", "Inheritance allows a class to inherit properties from a parent class."),
        ("What is polymorphism?", "ok"),  # Low quality response — should trigger alert
        ("What is encapsulation?", "Encapsulation bundles data and methods into a single unit."),
    ]

    for prompt, response in prompts_responses:
        result = await monitor.record(prompt, response, category="python-concepts")
        status = result.get("status", "warming_up")
        score = result.get("score", 0)
        print(f"[{status}] score={score:.1f} | {prompt[:40]}")

    print(f"\nSummary: {monitor.summary()}")

asyncio.run(demo_sqc_monitoring())
```

### Option 4: A/B Quality Comparison for Deployments

Before fully deploying a new model/prompt, compare quality on a sample of real traffic.

```python
import asyncio
import random
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class ABQualityTest:
    control_model: str
    treatment_model: str
    sample_size: int = 20
    quality_threshold_delta: float = -0.5  # Max acceptable quality drop

    results: list[dict] = field(default_factory=list)

    async def _score_response(self, prompt: str, response: str) -> float:
        judge = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            system="Rate response quality 1-10. Return ONLY the number.",
            messages=[{"role": "user", "content": f"Q: {prompt}\nA: {response[:200]}"}]
        )
        try:
            return float(judge.content[0].text.strip())
        except ValueError:
            return 5.0

    async def run_comparison(self, prompts: list[str]) -> dict:
        sample = random.sample(prompts, min(self.sample_size, len(prompts)))

        async def compare_one(prompt: str) -> dict:
            # Get both responses concurrently
            control_r, treatment_r = await asyncio.gather(
                client.messages.create(
                    model=self.control_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                ),
                client.messages.create(
                    model=self.treatment_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
            )

            control_text = control_r.content[0].text
            treatment_text = treatment_r.content[0].text

            # Score both
            control_score, treatment_score = await asyncio.gather(
                self._score_response(prompt, control_text),
                self._score_response(prompt, treatment_text)
            )

            return {
                "prompt": prompt[:50],
                "control_score": control_score,
                "treatment_score": treatment_score,
                "delta": treatment_score - control_score,
            }

        results = await asyncio.gather(*[compare_one(p) for p in sample])
        self.results = list(results)
        return self._analyze()

    def _analyze(self) -> dict:
        if not self.results:
            return {}

        avg_control = sum(r["control_score"] for r in self.results) / len(self.results)
        avg_treatment = sum(r["treatment_score"] for r in self.results) / len(self.results)
        avg_delta = avg_treatment - avg_control

        regression_detected = avg_delta < self.quality_threshold_delta
        worst_cases = sorted(self.results, key=lambda r: r["delta"])[:3]

        return {
            "control_avg": f"{avg_control:.2f}",
            "treatment_avg": f"{avg_treatment:.2f}",
            "avg_delta": f"{avg_delta:+.2f}",
            "regression_detected": regression_detected,
            "recommendation": "REJECT" if regression_detected else "APPROVE",
            "worst_regressions": [
                f"{r['prompt']} (delta={r['delta']:+.1f})"
                for r in worst_cases if r["delta"] < 0
            ],
        }

async def demo_ab_quality():
    prompts = [
        "What is a decorator in Python?",
        "Explain list comprehensions.",
        "What is the GIL?",
        "How does Python manage memory?",
        "What is duck typing?",
    ]

    # Compare same model against itself (delta should be ~0)
    test = ABQualityTest(
        control_model="claude-haiku-4-5-20251001",
        treatment_model="claude-haiku-4-5-20251001",
        sample_size=5,
    )

    print("Running A/B quality comparison...")
    analysis = await test.run_comparison(prompts)

    print(f"\nControl avg score: {analysis['control_avg']}")
    print(f"Treatment avg score: {analysis['treatment_avg']}")
    print(f"Average delta: {analysis['avg_delta']}")
    print(f"Regression detected: {analysis['regression_detected']}")
    print(f"Recommendation: {analysis['recommendation']}")

asyncio.run(demo_ab_quality())
```

### Option 5: Dimension-Specific Regression Rules

Define per-dimension regression thresholds and alert on specific quality attribute drops.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class RegressionRule:
    dimension: str
    threshold: float
    alert_level: str  # "warning" | "critical"
    description: str

REGRESSION_RULES = [
    RegressionRule("has_code_when_expected", 0.8, "critical",
                   "Code questions must include code blocks"),
    RegressionRule("response_completeness", 0.7, "critical",
                   "Response must address the full question"),
    RegressionRule("no_hallucinated_apis", 0.9, "critical",
                   "Must not reference non-existent Python APIs"),
    RegressionRule("appropriate_length", 0.7, "warning",
                   "Response length must match question complexity"),
    RegressionRule("professional_tone", 0.6, "warning",
                   "Must maintain professional technical tone"),
]

DIMENSION_EVALUATOR = """Evaluate this AI response on specific quality dimensions.

For each dimension, return a score 0.0-1.0:
- has_code_when_expected: Does it include code if the question asks for implementation?
- response_completeness: Does it address all parts of the question?
- no_hallucinated_apis: Does it avoid non-existent Python functions/methods?
- appropriate_length: Is length appropriate for the question complexity?
- professional_tone: Is the tone professional and technical?

Return JSON: {"has_code_when_expected": N, "response_completeness": N,
"no_hallucinated_apis": N, "appropriate_length": N, "professional_tone": N}"""

async def evaluate_dimensions(prompt: str, response: str) -> dict[str, float]:
    judge = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=DIMENSION_EVALUATOR,
        messages=[{"role": "user", "content": f"Q: {prompt}\nA: {response}"}]
    )
    try:
        return json.loads(judge.content[0].text)
    except json.JSONDecodeError:
        return {rule.dimension: 0.5 for rule in REGRESSION_RULES}

def check_regression_rules(scores: dict[str, float]) -> list[dict]:
    violations = []
    for rule in REGRESSION_RULES:
        score = scores.get(rule.dimension, 0.5)
        if score < rule.threshold:
            violations.append({
                "dimension": rule.dimension,
                "score": score,
                "threshold": rule.threshold,
                "level": rule.alert_level,
                "description": rule.description,
            })
    return violations

async def quality_gated_complete(prompt: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text
    scores = await evaluate_dimensions(prompt, text)
    violations = check_regression_rules(scores)

    critical = [v for v in violations if v["level"] == "critical"]
    warnings = [v for v in violations if v["level"] == "warning"]

    return {
        "response": text,
        "scores": scores,
        "violations": violations,
        "critical_count": len(critical),
        "warning_count": len(warnings),
        "quality_ok": len(critical) == 0,
    }

async def demo_dimension_rules():
    test_cases = [
        "Write a Python function that reads a JSON file.",
        "What is Python?",
        "How do I handle exceptions in async code?",
    ]

    for prompt in test_cases:
        result = await quality_gated_complete(prompt)
        status = "✓" if result["quality_ok"] else f"✗ ({result['critical_count']} critical)"
        print(f"\n[{status}] {prompt[:55]}")
        if result["violations"]:
            for v in result["violations"]:
                print(f"  [{v['level'].upper()}] {v['dimension']}: {v['score']:.2f} < {v['threshold']}")

asyncio.run(demo_dimension_rules())
```

### Option 6: Canary Quality Gate with Auto-Rollback

Route a fraction of traffic to the new model/prompt and auto-rollback if quality drops.

```python
import asyncio
import json
import random
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import deque

client = AsyncAnthropic()

@dataclass
class QualityCanary:
    control_model: str = "claude-haiku-4-5-20251001"
    canary_model: str = "claude-haiku-4-5-20251001"  # Replace with new model
    canary_traffic_pct: float = 0.1
    min_quality_score: float = 6.0
    rollback_threshold: float = 0.5  # Rollback if canary avg < this * control avg
    window: int = 20

    _control_scores: deque = field(default_factory=lambda: deque(maxlen=20))
    _canary_scores: deque = field(default_factory=lambda: deque(maxlen=20))
    _rolled_back: bool = False
    _rollback_triggered_at: float | None = None

    async def _score(self, prompt: str, response: str) -> float:
        judge = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            system="Score this response quality 1-10. Return ONLY the number.",
            messages=[{"role": "user", "content": f"Q: {prompt}\nA: {response[:150]}"}]
        )
        try:
            return float(judge.content[0].text.strip())
        except ValueError:
            return 5.0

    async def complete(self, prompt: str) -> tuple[str, str]:
        """Returns (response, variant)."""
        use_canary = (
            not self._rolled_back
            and random.random() < self.canary_traffic_pct
        )

        model = self.canary_model if use_canary else self.control_model
        variant = "canary" if use_canary else "control"

        response = await client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        score = await self._score(prompt, text)

        if use_canary:
            self._canary_scores.append(score)
        else:
            self._control_scores.append(score)

        await self._check_rollback()
        return text, variant

    async def _check_rollback(self):
        if self._rolled_back or len(self._canary_scores) < 5 or len(self._control_scores) < 5:
            return

        canary_avg = sum(self._canary_scores) / len(self._canary_scores)
        control_avg = sum(self._control_scores) / len(self._control_scores)

        quality_ratio = canary_avg / control_avg if control_avg > 0 else 1.0

        if quality_ratio < self.rollback_threshold:
            self._rolled_back = True
            self._rollback_triggered_at = time.monotonic()
            print(f"⚠ AUTO-ROLLBACK: canary avg={canary_avg:.2f} < "
                  f"{self.rollback_threshold:.0%} of control avg={control_avg:.2f}")

    def metrics(self) -> dict:
        canary_avg = (
            sum(self._canary_scores) / len(self._canary_scores)
            if self._canary_scores else None
        )
        control_avg = (
            sum(self._control_scores) / len(self._control_scores)
            if self._control_scores else None
        )
        return {
            "canary_samples": len(self._canary_scores),
            "control_samples": len(self._control_scores),
            "canary_avg_score": f"{canary_avg:.2f}" if canary_avg else "n/a",
            "control_avg_score": f"{control_avg:.2f}" if control_avg else "n/a",
            "rolled_back": self._rolled_back,
            "canary_traffic_pct": f"{self.canary_traffic_pct:.0%}",
        }

async def demo_canary_quality():
    canary = QualityCanary(canary_traffic_pct=0.3)

    prompts = [
        "What is Python?",
        "Explain generators.",
        "What is asyncio?",
        "How do decorators work?",
        "What is a context manager?",
        "Explain list comprehensions.",
        "What is duck typing?",
        "How does garbage collection work?",
    ]

    for prompt in prompts:
        response, variant = await canary.complete(prompt)
        print(f"[{variant}] {prompt[:40]}: {response.strip()[:60]}...")

    print(f"\nCanary metrics: {json.dumps(canary.metrics(), indent=2)}")

asyncio.run(demo_canary_quality())
```

## Comparison

| Approach | Detection Speed | False Positive Risk | Setup Effort | Best For |
|---|---|---|---|---|
| Reference Comparison | Per-request | Medium | Medium | Stable factual domains |
| LLM-as-Judge Scoring | Per-request | Low-Medium | Low | General quality monitoring |
| Statistical Process Control | After warm-up | Low | Medium | High-volume production traffic |
| A/B Quality Comparison | Pre-deployment | Low | Medium | Model/prompt upgrades |
| Dimension-Specific Rules | Per-request | Low | Medium | Domain-specific quality |
| Canary Quality Gate | Continuous | Low | High | Zero-downtime deployments |

**Choose LLM-as-Judge Scoring** as the foundation—it's the quickest to implement and works across all query types. **Choose Statistical Process Control** for production systems with high traffic where you need statistically principled alerting that auto-adjusts to baseline variance. **Choose Canary Quality Gate** whenever deploying model or prompt changes to production, as it catches regressions before they affect more than a small fraction of users.
