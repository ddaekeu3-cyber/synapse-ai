---
layout: solution
title: "Agent Doesn't Implement LLM-as-Judge Evaluation"
category: testing
description: "Without automated quality scoring, agent outputs are evaluated manually or not at all. LLM-as-Judge uses a second model call to score responses on criteria like accuracy, helpfulness, and safety — enabling continuous quality monitoring at scale."
tags: [testing, evaluation, llm-as-judge, quality, evals, scoring]
---

# Agent Doesn't Implement LLM-as-Judge Evaluation

## Problem

Manually evaluating agent outputs is slow and doesn't scale. Without automated quality measurement, regressions go undetected, prompt changes are shipped without quality gates, and there is no objective basis for comparing model versions. LLM-as-Judge addresses this by using a capable model to evaluate another model's outputs according to defined criteria.

## Why This Happens

Traditional unit tests check deterministic outputs. LLM outputs are probabilistic and require semantic understanding to evaluate. Teams defer to manual review because they lack tooling for automated semantic evaluation — and when they do build it, they often skip structured scoring rubrics, making results inconsistent.

## Solutions

### Option 1: Single-Criterion Judge — Score one quality dimension per call

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class JudgeScore:
    criterion: str
    score: int           # 1-5
    reasoning: str
    passed: bool         # score >= threshold

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.criterion}: {self.score}/5 — {self.reasoning}"


class SingleCriterionJudge:
    THRESHOLD = 3  # Minimum passing score

    def __init__(self, criterion: str, description: str):
        self.client = anthropic.Anthropic()
        self.criterion = criterion
        self.description = description

    def score(self, question: str, response: str) -> JudgeScore:
        prompt = f"""You are an expert evaluator. Score the following AI response on a single criterion.

CRITERION: {self.criterion}
DESCRIPTION: {self.description}

QUESTION ASKED:
{question}

AI RESPONSE:
{response}

Score the response on a scale of 1-5 where:
1 = Very poor — completely fails the criterion
2 = Poor — mostly fails the criterion
3 = Acceptable — partially meets the criterion
4 = Good — mostly meets the criterion
5 = Excellent — fully meets the criterion

Return JSON only: {{"score": 1-5, "reasoning": "brief explanation under 50 words"}}"""

        result = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            data = json.loads(result.content[0].text)
            score = int(data["score"])
            return JudgeScore(
                criterion=self.criterion,
                score=score,
                reasoning=data.get("reasoning", ""),
                passed=score >= self.THRESHOLD,
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            return JudgeScore(
                criterion=self.criterion, score=1,
                reasoning=f"Parse error: {e}", passed=False
            )


# Usage
client = anthropic.Anthropic()

# Generate an agent response
question = "Explain how garbage collection works in Python."
agent_response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": question}]
).content[0].text

# Evaluate on multiple single criteria
judges = [
    SingleCriterionJudge("Accuracy", "The response is factually correct."),
    SingleCriterionJudge("Clarity", "The response is easy to understand for a developer."),
    SingleCriterionJudge("Completeness", "The response covers the main aspects of the topic."),
]

print(f"Response:\n{agent_response}\n")
for judge in judges:
    result = judge.score(question, agent_response)
    print(result)

# Expected Token Savings: Judge model (Haiku) costs ~5-10% of generator (Sonnet); total overhead low
# Environment: Any production agent; integrate into CI to catch quality regressions on every deploy
```

### Option 2: Multi-Criteria Rubric Judge — Score all dimensions in one call

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class RubricResult:
    scores: dict[str, int]       # criterion -> 1-5
    reasoning: dict[str, str]    # criterion -> explanation
    overall: float               # weighted average
    passed: bool

    def report(self) -> str:
        lines = ["=== Evaluation Report ==="]
        for criterion, score in self.scores.items():
            lines.append(f"  {criterion}: {score}/5 — {self.reasoning.get(criterion, '')}")
        lines.append(f"  OVERALL: {self.overall:.1f}/5 ({'PASS' if self.passed else 'FAIL'})")
        return "\n".join(lines)


class RubricJudge:
    DEFAULT_RUBRIC = {
        "accuracy": ("Factual correctness", 0.35),
        "helpfulness": ("Addresses the user's actual need", 0.30),
        "clarity": ("Clear, readable, well-organized", 0.20),
        "conciseness": ("Avoids unnecessary verbosity", 0.15),
    }

    def __init__(
        self,
        rubric: dict[str, tuple[str, float]] | None = None,
        pass_threshold: float = 3.0,
        judge_model: str = "claude-haiku-4-5-20251001",
    ):
        self.client = anthropic.Anthropic()
        self.rubric = rubric or self.DEFAULT_RUBRIC
        self.pass_threshold = pass_threshold
        self.judge_model = judge_model

    def evaluate(
        self,
        question: str,
        response: str,
        context: str = "",
        reference_answer: str = "",
    ) -> RubricResult:
        rubric_text = "\n".join(
            f"- {name} (weight {weight:.0%}): {desc}"
            for name, (desc, weight) in self.rubric.items()
        )

        reference_section = f"\nREFERENCE ANSWER:\n{reference_answer}" if reference_answer else ""
        context_section = f"\nCONTEXT:\n{context}" if context else ""

        prompt = f"""You are an expert AI evaluator. Score this AI response according to the rubric below.

QUESTION:
{question}
{context_section}{reference_section}

AI RESPONSE:
{response}

SCORING RUBRIC (score each 1-5):
{rubric_text}

Return JSON only:
{{
  "scores": {{{", ".join(f'"{name}": 1' for name in self.rubric)}}},
  "reasoning": {{{", ".join(f'"{name}": "brief"' for name in self.rubric)}}}
}}"""

        result = self.client.messages.create(
            model=self.judge_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            data = json.loads(result.content[0].text)
            scores = {k: int(v) for k, v in data["scores"].items()}
            reasoning = data.get("reasoning", {})

            # Weighted average
            total_weight = sum(w for _, w in self.rubric.values())
            overall = sum(
                scores.get(name, 1) * weight
                for name, (_, weight) in self.rubric.items()
            ) / total_weight

            return RubricResult(
                scores=scores,
                reasoning=reasoning,
                overall=round(overall, 2),
                passed=overall >= self.pass_threshold,
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            return RubricResult(
                scores={}, reasoning={},
                overall=1.0, passed=False
            )


# Usage
client = anthropic.Anthropic()
judge = RubricJudge(pass_threshold=3.5)

question = "What is the difference between a list and a tuple in Python?"
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=256,
    messages=[{"role": "user", "content": question}]
).content[0].text

result = judge.evaluate(
    question=question,
    response=response,
    reference_answer="Lists are mutable, tuples are immutable. Lists use [], tuples use (). Tuples are hashable and can be dict keys."
)
print(result.report())

# Expected Token Savings: One Haiku call replaces N single-criterion calls; ~70% cheaper than separate judges
# Environment: Prompt regression suites, A/B testing, evaluation pipelines comparing model versions
```

### Option 3: Pairwise Comparison Judge — Rank two responses; find the better one

```python
import anthropic
import json
from dataclasses import dataclass
from enum import Enum

class Winner(Enum):
    A = "A"
    B = "B"
    TIE = "TIE"


@dataclass
class PairwiseResult:
    winner: Winner
    margin: str       # "clear", "slight", "tie"
    reasoning: str
    score_a: int      # 1-5
    score_b: int      # 1-5

    def summary(self) -> str:
        if self.winner == Winner.TIE:
            return f"TIE ({self.margin}) — {self.reasoning}"
        return f"Response {self.winner.value} wins ({self.margin}) — {self.reasoning}"


class PairwiseJudge:
    def __init__(self, judge_model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic()
        self.judge_model = judge_model

    def compare(
        self,
        question: str,
        response_a: str,
        response_b: str,
        criteria: str = "overall quality, accuracy, and helpfulness",
    ) -> PairwiseResult:
        # Randomize order to reduce position bias
        import random
        swap = random.random() > 0.5
        first = response_b if swap else response_a
        second = response_a if swap else response_b

        prompt = f"""You are an expert AI evaluator comparing two responses.

QUESTION: {question}

RESPONSE 1:
{first}

RESPONSE 2:
{second}

Evaluate both responses on: {criteria}

Return JSON only:
{{
  "winner": "1", "2", or "tie",
  "margin": "clear", "slight", or "tie",
  "score_1": 1-5,
  "score_2": 1-5,
  "reasoning": "one sentence explanation"
}}"""

        result = self.client.messages.create(
            model=self.judge_model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            data = json.loads(result.content[0].text)
            raw_winner = data.get("winner", "tie")
            score_1 = int(data.get("score_1", 3))
            score_2 = int(data.get("score_2", 3))

            # Un-swap
            if raw_winner == "1":
                actual_winner = Winner.B if swap else Winner.A
            elif raw_winner == "2":
                actual_winner = Winner.A if swap else Winner.B
            else:
                actual_winner = Winner.TIE

            score_a = score_2 if swap else score_1
            score_b = score_1 if swap else score_2

            return PairwiseResult(
                winner=actual_winner,
                margin=data.get("margin", "slight"),
                reasoning=data.get("reasoning", ""),
                score_a=score_a,
                score_b=score_b,
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return PairwiseResult(winner=Winner.TIE, margin="tie", reasoning="Parse error", score_a=3, score_b=3)


class PromptTournament:
    """Run multiple pairwise comparisons across prompt variants."""

    def __init__(self, question: str, responses: dict[str, str]):
        self.question = question
        self.responses = responses  # name -> response text
        self.judge = PairwiseJudge()
        self.wins: dict[str, int] = {name: 0 for name in responses}

    def run_all_pairs(self) -> dict:
        names = list(self.responses.keys())
        results = []

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                result = self.judge.compare(
                    question=self.question,
                    response_a=self.responses[a],
                    response_b=self.responses[b],
                )
                if result.winner == Winner.A:
                    self.wins[a] += 1
                elif result.winner == Winner.B:
                    self.wins[b] += 1
                results.append({"pair": f"{a} vs {b}", "result": result.summary()})

        ranking = sorted(self.wins.items(), key=lambda x: x[1], reverse=True)
        return {"ranking": ranking, "pairwise_results": results}


# Usage
client = anthropic.Anthropic()
question = "How do I reverse a string in Python?"

# Generate variants using different prompts
variants = {}
for name, system in [
    ("concise", "Be extremely brief."),
    ("detailed", "Provide thorough explanations with examples."),
    ("default", ""),
]:
    kwargs = {"system": system} if system else {}
    variants[name] = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=256,
        messages=[{"role": "user", "content": question}],
        **kwargs
    ).content[0].text

tournament = PromptTournament(question=question, responses=variants)
outcome = tournament.run_all_pairs()
print("RANKING:", outcome["ranking"])
for r in outcome["pairwise_results"]:
    print(f"  {r['pair']}: {r['result']}")

# Expected Token Savings: Identifies best prompt variant empirically; saves tokens long-term via better prompts
# Environment: Prompt A/B testing, model comparison, system prompt optimization
```

### Option 4: Factual Grounding Judge — Check response against provided ground truth

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class GroundingResult:
    supported_claims: list[str]
    unsupported_claims: list[str]
    contradicted_claims: list[str]
    grounding_score: float    # 0.0-1.0
    hallucination_detected: bool

    def report(self) -> str:
        lines = [
            f"Grounding Score: {self.grounding_score:.0%}",
            f"Hallucination: {'YES' if self.hallucination_detected else 'NO'}",
            f"Supported: {len(self.supported_claims)} claims",
            f"Unsupported: {len(self.unsupported_claims)} claims",
            f"Contradicted: {len(self.contradicted_claims)} claims",
        ]
        if self.contradicted_claims:
            lines.append("CONTRADICTIONS:")
            for c in self.contradicted_claims:
                lines.append(f"  - {c}")
        return "\n".join(lines)


class FactualGroundingJudge:
    def __init__(self, judge_model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic()
        self.judge_model = judge_model

    def evaluate(self, question: str, response: str, ground_truth: str) -> GroundingResult:
        prompt = f"""You are a fact-checking evaluator. Analyze the AI response against the ground truth.

QUESTION: {question}

GROUND TRUTH (authoritative source):
{ground_truth}

AI RESPONSE TO EVALUATE:
{response}

Tasks:
1. Extract all factual claims from the AI response
2. Classify each claim as:
   - "supported": directly supported by ground truth
   - "unsupported": not found in ground truth (potential hallucination)
   - "contradicted": explicitly contradicts ground truth

Return JSON only:
{{
  "supported": ["claim1", "claim2"],
  "unsupported": ["claim3"],
  "contradicted": ["claim4"]
}}"""

        result = self.client.messages.create(
            model=self.judge_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            data = json.loads(result.content[0].text)
            supported = data.get("supported", [])
            unsupported = data.get("unsupported", [])
            contradicted = data.get("contradicted", [])

            total = len(supported) + len(unsupported) + len(contradicted)
            score = len(supported) / total if total > 0 else 1.0

            return GroundingResult(
                supported_claims=supported,
                unsupported_claims=unsupported,
                contradicted_claims=contradicted,
                grounding_score=round(score, 2),
                hallucination_detected=len(contradicted) > 0 or (len(unsupported) / max(total, 1)) > 0.3,
            )
        except (json.JSONDecodeError, KeyError):
            return GroundingResult([], [], [], 0.0, True)


# Usage
client = anthropic.Anthropic()
judge = FactualGroundingJudge()

question = "What is the GIL in Python and how does it affect threading?"
ground_truth = """The Global Interpreter Lock (GIL) is a mutex in CPython that prevents multiple native threads from executing Python bytecodes simultaneously. It was introduced to simplify memory management in CPython's reference counting GC. For CPU-bound tasks, the GIL limits parallelism to one thread at a time. For I/O-bound tasks, threads can release the GIL while waiting. The GIL does not affect multiprocessing. Python 3.13 adds an option to disable the GIL (PEP 703)."""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": question}]
).content[0].text

result = judge.evaluate(question, response, ground_truth)
print(result.report())

# Expected Token Savings: Catches hallucinations before users do; prevents costly corrections downstream
# Environment: RAG systems, knowledge base QA, any domain where factual accuracy is critical
```

### Option 5: Async Batch Evaluator — Evaluate many responses in parallel

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass, field

@dataclass
class EvalCase:
    id: str
    question: str
    response: str
    reference: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    case_id: str
    score: float
    passed: bool
    breakdown: dict[str, int]
    error: str = ""


class AsyncBatchEvaluator:
    """Evaluate multiple cases concurrently using async API."""

    CONCURRENCY = 5  # Max parallel judge calls
    PASS_THRESHOLD = 3.2

    def __init__(self, judge_model: str = "claude-haiku-4-5-20251001"):
        self.client = anthropic.AsyncAnthropic()
        self.judge_model = judge_model
        self._semaphore = asyncio.Semaphore(self.CONCURRENCY)

    async def _evaluate_single(self, case: EvalCase) -> EvalResult:
        async with self._semaphore:
            reference_line = f"\nREFERENCE: {case.reference}" if case.reference else ""
            prompt = f"""Score this AI response on 3 criteria (1-5 each).

QUESTION: {case.question}{reference_line}

RESPONSE: {case.response}

Return JSON: {{"accuracy": 1-5, "clarity": 1-5, "helpfulness": 1-5}}"""

            try:
                result = await self.client.messages.create(
                    model=self.judge_model,
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}]
                )
                data = json.loads(result.content[0].text)
                breakdown = {k: int(v) for k, v in data.items()}
                avg = sum(breakdown.values()) / len(breakdown)
                return EvalResult(
                    case_id=case.id,
                    score=round(avg, 2),
                    passed=avg >= self.PASS_THRESHOLD,
                    breakdown=breakdown,
                )
            except Exception as e:
                return EvalResult(case_id=case.id, score=0.0, passed=False, breakdown={}, error=str(e))

    async def evaluate_all(self, cases: list[EvalCase]) -> list[EvalResult]:
        tasks = [self._evaluate_single(case) for case in cases]
        return await asyncio.gather(*tasks)

    def summary(self, results: list[EvalResult]) -> dict:
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]
        scores = [r.score for r in results if not r.error]
        return {
            "total": len(results),
            "passed": len(passed),
            "failed": len(failed),
            "pass_rate": f"{len(passed)/len(results)*100:.0f}%",
            "avg_score": round(sum(scores)/len(scores), 2) if scores else 0,
            "failed_cases": [r.case_id for r in failed],
        }


async def main():
    client = anthropic.AsyncAnthropic()
    evaluator = AsyncBatchEvaluator()

    # Generate test responses
    questions = [
        ("q1", "What is a Python decorator?"),
        ("q2", "Explain async/await in Python."),
        ("q3", "What is the difference between == and is in Python?"),
        ("q4", "How does Python's memory management work?"),
        ("q5", "What is a Python generator?"),
    ]

    cases = []
    for qid, q in questions:
        response = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=256,
            messages=[{"role": "user", "content": q}]
        )
        cases.append(EvalCase(id=qid, question=q, response=response.content[0].text))

    # Evaluate all in parallel
    results = await evaluator.evaluate_all(cases)
    for r in results:
        status = "✓" if r.passed else "✗"
        print(f"[{status}] {r.case_id}: {r.score}/5 {r.breakdown}")

    print("\nSUMMARY:", evaluator.summary(results))


asyncio.run(main())

# Expected Token Savings: Parallel evaluation; 5x throughput vs sequential; Haiku judge = low per-case cost
# Environment: CI/CD eval gates, nightly regression runs, large golden-set evaluations
```

### Option 6: Weighted Eval Suite — Multi-judge pipeline with weighted quality gates

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class EvalDimension:
    name: str
    prompt_template: str
    weight: float
    pass_threshold: int = 3


@dataclass
class SuiteResult:
    dimension_scores: dict[str, int]
    weighted_score: float
    passed: bool
    gate_failures: list[str]

    def report(self) -> str:
        lines = ["=== Eval Suite Result ==="]
        for dim, score in self.dimension_scores.items():
            lines.append(f"  {dim}: {score}/5")
        lines.append(f"  Weighted Score: {self.weighted_score:.2f}/5")
        lines.append(f"  Status: {'PASS' if self.passed else 'FAIL'}")
        if self.gate_failures:
            lines.append(f"  Failed gates: {', '.join(self.gate_failures)}")
        return "\n".join(lines)


class WeightedEvalSuite:
    """Multi-dimension evaluator with per-dimension pass gates."""

    DIMENSIONS = [
        EvalDimension(
            name="accuracy",
            prompt_template="Is the response factually correct? Question: {q}\nResponse: {r}\nReturn JSON: {{\"score\": 1-5}}",
            weight=0.4,
            pass_threshold=3,
        ),
        EvalDimension(
            name="safety",
            prompt_template="Does the response avoid harmful, biased, or inappropriate content? Response: {r}\nReturn JSON: {{\"score\": 1-5}}",
            weight=0.3,
            pass_threshold=4,  # Higher bar for safety
        ),
        EvalDimension(
            name="helpfulness",
            prompt_template="Does the response actually help the user? Question: {q}\nResponse: {r}\nReturn JSON: {{\"score\": 1-5}}",
            weight=0.3,
            pass_threshold=3,
        ),
    ]

    def __init__(self, judge_model: str = "claude-haiku-4-5-20251001"):
        self.client = anthropic.Anthropic()
        self.judge_model = judge_model

    def _score_dimension(self, dim: EvalDimension, question: str, response: str) -> int:
        prompt = dim.prompt_template.format(q=question, r=response)
        result = self.client.messages.create(
            model=self.judge_model,
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}]
        )
        try:
            data = json.loads(result.content[0].text)
            return int(data.get("score", 1))
        except (json.JSONDecodeError, KeyError, ValueError):
            return 1

    def evaluate(self, question: str, response: str) -> SuiteResult:
        scores: dict[str, int] = {}
        gate_failures: list[str] = []

        for dim in self.DIMENSIONS:
            score = self._score_dimension(dim, question, response)
            scores[dim.name] = score
            if score < dim.pass_threshold:
                gate_failures.append(f"{dim.name}={score}<{dim.pass_threshold}")

        total_weight = sum(d.weight for d in self.DIMENSIONS)
        weighted = sum(
            scores[d.name] * d.weight for d in self.DIMENSIONS
        ) / total_weight

        return SuiteResult(
            dimension_scores=scores,
            weighted_score=round(weighted, 2),
            passed=len(gate_failures) == 0,
            gate_failures=gate_failures,
        )


# Usage
client = anthropic.Anthropic()
suite = WeightedEvalSuite()

test_cases = [
    "What is the capital of France?",
    "How do I sort a list in Python?",
    "Explain the concept of recursion.",
]

for question in test_cases:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": question}]
    ).content[0].text

    result = suite.evaluate(question, response)
    print(f"\nQ: {question}")
    print(result.report())

# Expected Token Savings: Catches failing responses early; gates prevent expensive downstream processing
# Environment: Production quality gates, compliance checks, safety-critical agent deployments
```

## Comparison

| Option | Dimensions | Cost/Eval | Position Bias | Best For |
|--------|-----------|-----------|---------------|----------|
| Single-Criterion | 1 | Very Low | None | Quick spot checks, targeted testing |
| Multi-Criteria Rubric | 4+ | Low | None | Comprehensive regression suites |
| Pairwise Comparison | Relative | Medium | Mitigated (swap) | A/B testing, model selection |
| Factual Grounding | Claims-based | Medium | None | RAG, knowledge QA, factuality |
| Async Batch | 3 | Low (parallel) | None | CI pipelines, large eval sets |
| Weighted Suite | 3 (gated) | Low | None | Production quality gates, safety |
