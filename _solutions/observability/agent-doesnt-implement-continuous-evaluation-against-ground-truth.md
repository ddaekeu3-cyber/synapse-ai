---
title: "Agent Doesn't Implement Continuous Evaluation Against Ground Truth"
description: "AI agents deployed without continuous evaluation regress silently—prompt changes, model updates, or retrieval drift degrade output quality weeks before anyone notices in user complaints. Continuous evaluation runs a curated golden dataset through the agent on every deployment and computes recall, precision, and semantic similarity scores, alerting when any metric falls below threshold."
date: 2025-02-20
difficulty: advanced
category: observability
slug: agent-doesnt-implement-continuous-evaluation-against-ground-truth
tags:
  - evaluation
  - ground-truth
  - llm-eval
  - regression-detection
  - quality-monitoring
  - continuous-evaluation
  - golden-dataset
symptoms:
  - "Agent answer quality degraded for two weeks before a user complaint surfaced the regression"
  - "Prompt change that fixed one category silently broke three others with no metric alert"
  - "Model version upgrade changed tone and factual accuracy but passed all unit tests"
  - "No baseline exists to compare before/after impact of retrieval parameter changes"
  - "Quality review is manual: engineers spot-check 10 examples before each deployment"
---

## Problem

Unit tests verify that code runs without crashing, not that the agent produces high-quality answers. When a system prompt is reworded, a retrieval threshold is changed, or the underlying model is upgraded, answer quality can shift in subtle ways—correct format, wrong facts; correct facts, wrong tone; correct answer for common cases, broken for edge cases. Without automated evaluation against a ground-truth dataset, these regressions surface only through user complaints or manual review. Continuous evaluation computes objective metrics (exact match, F1, ROUGE, semantic similarity) on a golden test set after every deployment and blocks promotion when metrics drop below configured thresholds.

---

## Solution 1: GoldenDataset — Curated Test Cases with Expected Outputs

```python
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class EvalCase:
    case_id: str
    input: Dict[str, Any]          # query, context, conversation history, etc.
    expected_output: str           # canonical correct answer
    expected_facts: List[str] = field(default_factory=list)   # must-contain substrings
    forbidden_content: List[str] = field(default_factory=list) # must-not-contain
    category: str = ""             # group for per-category metric breakdown
    difficulty: str = "medium"     # easy / medium / hard
    metadata: Dict[str, Any] = field(default_factory=dict)


class GoldenDataset:
    """
    Loads and manages a curated set of (input, expected_output) pairs
    for continuous evaluation. Supports JSONL format for easy version
    control alongside the prompt files that the cases test.

    Usage:
        dataset = GoldenDataset.from_jsonl("evals/golden.jsonl")
        print(f"Loaded {len(dataset)} cases across {dataset.categories}")
        for case in dataset.by_category("factual"):
            run_eval(case)
    """

    def __init__(self, cases: List[EvalCase]):
        self._cases = cases

    @classmethod
    def from_jsonl(cls, path: str) -> "GoldenDataset":
        cases = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                cases.append(EvalCase(
                    case_id=d["case_id"],
                    input=d["input"],
                    expected_output=d["expected_output"],
                    expected_facts=d.get("expected_facts", []),
                    forbidden_content=d.get("forbidden_content", []),
                    category=d.get("category", ""),
                    difficulty=d.get("difficulty", "medium"),
                    metadata=d.get("metadata", {}),
                ))
        return cls(cases)

    @classmethod
    def from_list(cls, cases: List[Dict]) -> "GoldenDataset":
        return cls([EvalCase(**c) for c in cases])

    def by_category(self, category: str) -> List[EvalCase]:
        return [c for c in self._cases if c.category == category]

    def by_difficulty(self, difficulty: str) -> List[EvalCase]:
        return [c for c in self._cases if c.difficulty == difficulty]

    def to_jsonl(self, path: str):
        with open(path, "w") as f:
            for case in self._cases:
                f.write(json.dumps({
                    "case_id": case.case_id,
                    "input": case.input,
                    "expected_output": case.expected_output,
                    "expected_facts": case.expected_facts,
                    "forbidden_content": case.forbidden_content,
                    "category": case.category,
                    "difficulty": case.difficulty,
                    "metadata": case.metadata,
                }) + "\n")

    @property
    def categories(self) -> List[str]:
        return sorted(set(c.category for c in self._cases))

    def __len__(self) -> int:
        return len(self._cases)

    def __iter__(self):
        return iter(self._cases)
```

---

## Solution 2: EvalMetrics — Scoring Functions for Agent Outputs

```python
import re
from typing import List, Optional


class EvalMetrics:
    """
    Collection of evaluation metrics for agent text output:
    exact match, F1 token overlap, fact coverage, forbidden content
    detection, and semantic similarity (via sentence-transformers).

    Usage:
        score = EvalMetrics.f1_score(predicted="Paris is the capital",
                                      expected="The capital is Paris")
        coverage = EvalMetrics.fact_coverage(
            predicted="The speed of light is 299,792 km/s",
            facts=["299,792", "km/s"]
        )
    """

    @staticmethod
    def normalize(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^\w\s]", " ", text)
        return " ".join(text.split())

    @staticmethod
    def exact_match(predicted: str, expected: str) -> float:
        return 1.0 if EvalMetrics.normalize(predicted) == EvalMetrics.normalize(expected) else 0.0

    @staticmethod
    def f1_score(predicted: str, expected: str) -> float:
        pred_tokens = set(EvalMetrics.normalize(predicted).split())
        exp_tokens = set(EvalMetrics.normalize(expected).split())
        if not pred_tokens or not exp_tokens:
            return 0.0
        common = pred_tokens & exp_tokens
        if not common:
            return 0.0
        precision = len(common) / len(pred_tokens)
        recall = len(common) / len(exp_tokens)
        return 2 * precision * recall / (precision + recall)

    @staticmethod
    def fact_coverage(predicted: str, facts: List[str]) -> float:
        """Fraction of expected_facts substrings present in predicted."""
        if not facts:
            return 1.0
        normalized = predicted.lower()
        covered = sum(1 for fact in facts if fact.lower() in normalized)
        return covered / len(facts)

    @staticmethod
    def forbidden_fraction(predicted: str, forbidden: List[str]) -> float:
        """Fraction of forbidden_content substrings present (0 = clean)."""
        if not forbidden:
            return 0.0
        normalized = predicted.lower()
        found = sum(1 for f in forbidden if f.lower() in normalized)
        return found / len(forbidden)

    @staticmethod
    def rouge_l(predicted: str, expected: str) -> float:
        """Longest common subsequence-based ROUGE-L score."""
        pred_tokens = EvalMetrics.normalize(predicted).split()
        exp_tokens = EvalMetrics.normalize(expected).split()
        m, n = len(pred_tokens), len(exp_tokens)
        if m == 0 or n == 0:
            return 0.0
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if pred_tokens[i - 1] == exp_tokens[j - 1]:
                    dp[i][j] = dp[i - 1][j - 1] + 1
                else:
                    dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
        lcs = dp[m][n]
        precision = lcs / m
        recall = lcs / n
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    @staticmethod
    def semantic_similarity(predicted: str, expected: str) -> float:
        """Cosine similarity via sentence-transformers. Returns -1 if unavailable."""
        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np
            model = SentenceTransformer("all-MiniLM-L6-v2")
            vecs = model.encode([predicted, expected])
            cos = float(np.dot(vecs[0], vecs[1]) /
                         (np.linalg.norm(vecs[0]) * np.linalg.norm(vecs[1]) + 1e-9))
            return round(cos, 4)
        except ImportError:
            return -1.0
```

---

## Solution 3: ContinuousEvaluator — Run Golden Dataset Through Agent

```python
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CaseResult:
    case_id: str
    category: str
    predicted: str
    exact_match: float
    f1: float
    fact_coverage: float
    forbidden_fraction: float
    rouge_l: float
    latency_ms: float
    error: Optional[str] = None


@dataclass
class EvalReport:
    run_id: str
    timestamp: float
    total_cases: int
    error_count: int
    results: List[CaseResult] = field(default_factory=list)

    def mean(self, metric: str) -> float:
        vals = [getattr(r, metric) for r in self.results if getattr(r, metric, None) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def by_category(self) -> Dict[str, Dict[str, float]]:
        cats: Dict[str, List[CaseResult]] = {}
        for r in self.results:
            cats.setdefault(r.category, []).append(r)
        return {
            cat: {
                "f1": sum(r.f1 for r in rs) / len(rs),
                "fact_coverage": sum(r.fact_coverage for r in rs) / len(rs),
                "exact_match": sum(r.exact_match for r in rs) / len(rs),
                "count": len(rs),
            }
            for cat, rs in cats.items()
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total_cases": self.total_cases,
            "error_count": self.error_count,
            "mean_f1": round(self.mean("f1"), 4),
            "mean_fact_coverage": round(self.mean("fact_coverage"), 4),
            "mean_rouge_l": round(self.mean("rouge_l"), 4),
            "mean_latency_ms": round(self.mean("latency_ms"), 1),
            "by_category": self.by_category(),
        }


class ContinuousEvaluator:
    """
    Runs every case in a GoldenDataset through the agent and computes
    aggregate metrics. Designed to run in CI after deployment or on a
    schedule. Concurrency is bounded to avoid overwhelming the LLM API.

    Usage:
        evaluator = ContinuousEvaluator(agent_fn=my_agent.run, concurrency=5)
        report = await evaluator.run(dataset, run_id="deploy-v2.3.1")
        if report.mean("f1") < 0.75:
            raise RuntimeError("Eval regression: F1 below threshold")
    """

    def __init__(self, agent_fn: Callable, concurrency: int = 5):
        self._agent = agent_fn
        self._sem = asyncio.Semaphore(concurrency)

    async def _eval_case(self, case) -> CaseResult:
        async with self._sem:
            t0 = time.monotonic()
            try:
                predicted = await self._agent(case.input) \
                    if asyncio.iscoroutinefunction(self._agent) \
                    else self._agent(case.input)
                predicted = str(predicted)
                error = None
            except Exception as exc:
                predicted = ""
                error = str(exc)
            latency_ms = round((time.monotonic() - t0) * 1000, 1)

            return CaseResult(
                case_id=case.case_id,
                category=case.category,
                predicted=predicted,
                exact_match=EvalMetrics.exact_match(predicted, case.expected_output),
                f1=EvalMetrics.f1_score(predicted, case.expected_output),
                fact_coverage=EvalMetrics.fact_coverage(predicted, case.expected_facts),
                forbidden_fraction=EvalMetrics.forbidden_fraction(predicted, case.forbidden_content),
                rouge_l=EvalMetrics.rouge_l(predicted, case.expected_output),
                latency_ms=latency_ms,
                error=error,
            )

    async def run(self, dataset, run_id: str = "") -> EvalReport:
        import uuid
        run_id = run_id or str(uuid.uuid4())[:8]
        logger.info("eval_run_start run_id=%s cases=%d", run_id, len(dataset))
        tasks = [self._eval_case(case) for case in dataset]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        error_count = sum(1 for r in results if r.error)
        report = EvalReport(
            run_id=run_id,
            timestamp=time.time(),
            total_cases=len(results),
            error_count=error_count,
            results=list(results),
        )
        summary = report.summary()
        logger.info("eval_run_complete %s", summary)
        return report
```

---

## Solution 4: EvalThresholdGuard — Block Deployment on Regression

```python
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ThresholdConfig:
    min_f1: float = 0.70
    min_fact_coverage: float = 0.85
    min_exact_match: float = 0.0    # often 0 for open-ended answers
    max_forbidden_fraction: float = 0.0
    max_error_rate: float = 0.05
    per_category_min_f1: Optional[Dict[str, float]] = None


@dataclass
class ThresholdViolation:
    metric: str
    actual: float
    threshold: float
    category: Optional[str] = None

    def __str__(self):
        cat = f" (category={self.category})" if self.category else ""
        return f"{self.metric}{cat}: {self.actual:.4f} < {self.threshold:.4f}"


class EvalThresholdGuard:
    """
    Compares an EvalReport against configured thresholds and raises
    if any threshold is violated. Integrate into CI/CD deployment gates.

    Usage:
        guard = EvalThresholdGuard(ThresholdConfig(min_f1=0.75, min_fact_coverage=0.90))
        violations = guard.check(report)
        if violations:
            raise SystemExit(f"Eval gate failed: {violations}")
    """

    def __init__(self, config: ThresholdConfig):
        self._config = config

    def check(self, report) -> List[ThresholdViolation]:
        violations = []
        cfg = self._config

        def check_metric(metric, actual, threshold, category=None):
            if actual < threshold:
                violations.append(ThresholdViolation(metric, actual, threshold, category))

        check_metric("f1", report.mean("f1"), cfg.min_f1)
        check_metric("fact_coverage", report.mean("fact_coverage"), cfg.min_fact_coverage)
        check_metric("exact_match", report.mean("exact_match"), cfg.min_exact_match)

        actual_forbidden = report.mean("forbidden_fraction")
        if actual_forbidden > cfg.max_forbidden_fraction:
            violations.append(ThresholdViolation(
                "forbidden_fraction", actual_forbidden, cfg.max_forbidden_fraction
            ))

        actual_error_rate = report.error_count / max(report.total_cases, 1)
        if actual_error_rate > cfg.max_error_rate:
            violations.append(ThresholdViolation(
                "error_rate", actual_error_rate, cfg.max_error_rate
            ))

        if cfg.per_category_min_f1:
            by_cat = report.by_category()
            for category, min_f1 in cfg.per_category_min_f1.items():
                actual = by_cat.get(category, {}).get("f1", 0.0)
                check_metric("f1", actual, min_f1, category=category)

        for v in violations:
            logger.error("eval_threshold_violation %s", v)
        return violations

    def assert_passing(self, report):
        violations = self.check(report)
        if violations:
            raise AssertionError(
                "Eval gate failed with violations:\n" +
                "\n".join(f"  - {v}" for v in violations)
            )
```

---

## Solution 5: EvalResultStore — Persist and Compare Runs Over Time

```python
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EvalResultStore:
    """
    Persists EvalReport summaries to a JSONL file and supports trend
    analysis: compute metric delta vs. the previous run to detect regressions
    and improvements over time.

    Usage:
        store = EvalResultStore("/var/lib/agent/eval-results.jsonl")
        store.save(report)
        delta = store.delta_vs_previous(report, metric="f1")
        if delta < -0.02:
            alert(f"F1 dropped {delta:.4f} vs last run")
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, report) -> None:
        summary = report.summary()
        summary["ts"] = time.time()
        with open(self._path, "a") as f:
            f.write(json.dumps(summary) + "\n")
        logger.info("eval_result_saved run_id=%s", report.run_id)

    def load_all(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        results = []
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if line:
                    results.append(json.loads(line))
        return sorted(results, key=lambda x: x.get("ts", 0))

    def last_n(self, n: int = 10) -> List[Dict[str, Any]]:
        return self.load_all()[-n:]

    def delta_vs_previous(self, report, metric: str = "f1") -> Optional[float]:
        history = self.load_all()
        if len(history) < 2:
            return None
        prev = history[-2]
        curr_val = report.summary().get(f"mean_{metric}", 0.0)
        prev_val = prev.get(f"mean_{metric}", 0.0)
        return round(curr_val - prev_val, 6)

    def trend(self, metric: str = "mean_f1", last_n: int = 20) -> List[Dict]:
        return [
            {"run_id": r.get("run_id"), "ts": r.get("ts"), metric: r.get(metric)}
            for r in self.last_n(last_n)
        ]
```

---

## Solution 6: EvalPipeline — End-to-End CI/CD Evaluation Orchestration

```python
import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class EvalPipeline:
    """
    Orchestrates the full continuous evaluation workflow:
    load dataset -> run evaluator -> check thresholds -> store results -> emit metrics.
    Designed to run as a post-deployment step in CI/CD or as a scheduled job.

    Usage:
        pipeline = EvalPipeline(
            agent_fn=my_agent.run,
            dataset_path="evals/golden.jsonl",
            results_path="/var/lib/agent/eval-results.jsonl",
            thresholds=ThresholdConfig(min_f1=0.75),
            metrics_pusher=prometheus_push,   # optional
        )
        passed = await pipeline.run(run_id=os.environ.get("GIT_SHA", ""))
        sys.exit(0 if passed else 1)
    """

    def __init__(
        self,
        agent_fn: Callable,
        dataset_path: str,
        results_path: str,
        thresholds: Optional[Any] = None,
        concurrency: int = 5,
        metrics_pusher: Optional[Callable] = None,
    ):
        self._agent = agent_fn
        self._dataset_path = dataset_path
        self._results_path = results_path
        self._thresholds = thresholds or ThresholdConfig()
        self._concurrency = concurrency
        self._metrics_pusher = metrics_pusher

    async def run(self, run_id: str = "") -> bool:
        t0 = time.monotonic()
        dataset = GoldenDataset.from_jsonl(self._dataset_path)
        evaluator = ContinuousEvaluator(self._agent, concurrency=self._concurrency)
        report = await evaluator.run(dataset, run_id=run_id)

        store = EvalResultStore(self._results_path)
        store.save(report)

        delta = store.delta_vs_previous(report, "f1")
        if delta is not None:
            logger.info("eval_f1_delta run_id=%s delta=%.4f", run_id, delta)

        guard = EvalThresholdGuard(self._thresholds)
        violations = guard.check(report)

        summary = report.summary()
        elapsed = round((time.monotonic() - t0) * 1000)
        logger.info("eval_pipeline_complete run_id=%s elapsed_ms=%d summary=%s",
                     run_id, elapsed, summary)

        if self._metrics_pusher:
            try:
                self._metrics_pusher(summary)
            except Exception as exc:
                logger.warning("eval_metrics_push_failed error=%s", exc)

        return len(violations) == 0
```

---

## Comparison

| Approach | Test Data | Metrics | CI/CD Gate | Trend Tracking | Category Breakdown | Integrated |
|---|---|---|---|---|---|---|
| **GoldenDataset** | JSONL loader | N/A | No | No | Yes | No |
| **EvalMetrics** | N/A | F1, ROUGE, facts | No | No | No | No |
| **ContinuousEvaluator** | Via dataset | All metrics | No | No | Yes | No |
| **EvalThresholdGuard** | Via report | Threshold check | Yes | No | Yes | No |
| **EvalResultStore** | Via report | Trend delta | No | Yes | No | No |
| **EvalPipeline** | All | All | Yes | Yes | Yes | Yes |

**Key insight**: start by building a `GoldenDataset` of 50-100 cases covering your most important use cases—this is the highest-leverage investment. Even without automated CI gates, running `ContinuousEvaluator` manually before and after every prompt change catches regressions in minutes instead of weeks. Add `EvalThresholdGuard` to block deployments when mean F1 drops below 0.70 or fact coverage drops below 0.85. Track `delta_vs_previous` over time: a consistent -0.5% F1 per week signals retrieval drift before it becomes a user-visible problem.
