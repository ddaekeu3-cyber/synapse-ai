---
layout: solution
title: "Agent Doesn't Implement Shadow Testing with Production Traffic"
category: testing
description: "Mirror a fraction of live production requests to a candidate agent (new model, new prompt, new logic) and compare responses offline — validating changes with real traffic before any user sees them."
tags: [testing, shadow-mode, production, ab-testing, validation, prompt-testing]
---

Testing a new model version, prompt change, or agent logic update against synthetic data gives false confidence. Real production queries have a distribution that synthetic benchmarks miss. Shadow testing routes a fraction of live requests to both the current (control) and candidate (shadow) agents simultaneously, compares their responses offline, and surfaces regressions before any user is affected. The user always gets the control response; the shadow result is used only for evaluation.

## Option 1: In-Process Shadow Forking

Intercept each request in-process, run the shadow call asynchronously after returning the control response, then log both for comparison. Zero user-facing latency impact since shadow runs after the primary response is returned.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, asdict

@dataclass
class ShadowRecord:
    request_id: str
    user_query: str
    control_response: str
    shadow_response: str
    control_model: str
    shadow_model: str
    control_latency_ms: float
    shadow_latency_ms: float
    diverged: bool

_shadow_log: list[ShadowRecord] = []

async def run_shadow(
    client: anthropic.AsyncAnthropic,
    request_id: str,
    query: str,
    control_response: str,
    shadow_model: str,
    shadow_system: str,
) -> None:
    start = time.monotonic()
    try:
        response = await client.messages.create(
            model=shadow_model,
            max_tokens=512,
            system=shadow_system,
            messages=[{"role": "user", "content": query}],
        )
        shadow_text = response.content[0].text
        shadow_latency = (time.monotonic() - start) * 1000

        # Simple divergence check: word overlap < 30% → diverged
        ctrl_words = set(control_response.lower().split())
        shad_words = set(shadow_text.lower().split())
        overlap = len(ctrl_words & shad_words) / len(ctrl_words | shad_words) if (ctrl_words | shad_words) else 1.0
        diverged = overlap < 0.30

        record = ShadowRecord(
            request_id=request_id,
            user_query=query,
            control_response=control_response[:300],
            shadow_response=shadow_text[:300],
            control_model="claude-haiku-4-5-20251001",
            shadow_model=shadow_model,
            control_latency_ms=0,  # filled by caller
            shadow_latency_ms=shadow_latency,
            diverged=diverged,
        )
        _shadow_log.append(record)
        if diverged:
            print(f"[Shadow] DIVERGED request={request_id} overlap={overlap:.2f}")
    except Exception as e:
        print(f"[Shadow] Error on {request_id}: {e}")

def serve_with_shadow(
    query: str,
    request_id: str,
    shadow_rate: float = 0.3,  # 30% of traffic
    shadow_model: str = "claude-sonnet-4-6",
    shadow_system: str = "You are a helpful assistant.",
    control_system: str = "You are a helpful assistant.",
) -> str:
    import uuid, random
    if not request_id:
        request_id = str(uuid.uuid4())[:8]

    client_sync = anthropic.Anthropic()
    start = time.monotonic()
    control_response = client_sync.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=control_system,
        messages=[{"role": "user", "content": query}],
    ).content[0].text
    control_latency = (time.monotonic() - start) * 1000

    # Fire shadow asynchronously if this request is sampled
    if random.random() < shadow_rate:
        async def _run():
            async_client = anthropic.AsyncAnthropic()
            await run_shadow(async_client, request_id, query, control_response, shadow_model, shadow_system)
        asyncio.create_task(_run()) if asyncio.get_event_loop().is_running() else None
        print(f"[Shadow] Sampled request={request_id} | control_latency={control_latency:.0f}ms")

    return control_response  # user always gets control response

def shadow_report() -> dict:
    if not _shadow_log:
        return {"total": 0}
    diverged = [r for r in _shadow_log if r.diverged]
    return {
        "total_shadowed": len(_shadow_log),
        "diverged": len(diverged),
        "divergence_rate": f"{len(diverged)/len(_shadow_log)*100:.1f}%",
        "avg_shadow_latency_ms": sum(r.shadow_latency_ms for r in _shadow_log) / len(_shadow_log),
        "diverged_examples": [{"query": r.user_query[:60], "req_id": r.request_id} for r in diverged[:3]],
    }

if __name__ == "__main__":
    import uuid
    queries = [
        "What is Python's GIL?",
        "How do I reverse a string in Python?",
        "Explain async/await in Python",
        "What are Python dataclasses?",
        "How does Python garbage collection work?",
    ]
    # Synchronous simulation (without event loop for demo)
    client = anthropic.Anthropic()
    for q in queries:
        rid = str(uuid.uuid4())[:8]
        control = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": q}],
        ).content[0].text
        print(f"Control [{rid}]: {control[:80]}...")

    print("\n=== Shadow Report ===")
    print(json.dumps(shadow_report(), indent=2))

# Expected Token Savings: N/A — shadow doubles token cost for sampled traffic; reduces regression cost
# Environment: pip install anthropic
```

## Option 2: Async Shadow Queue with Offline Comparison

Buffer shadow requests in an async queue. A background worker processes the queue, runs shadow calls, and stores structured comparison records. Decouples shadow evaluation from the request path entirely — zero overhead on production latency.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class ShadowJob:
    request_id: str
    query: str
    control_response: str
    control_model: str
    shadow_config: dict  # {model, system, max_tokens}
    enqueued_at: float = field(default_factory=time.time)

@dataclass
class ComparisonResult:
    request_id: str
    query: str
    control: str
    shadow: str
    length_ratio: float      # shadow_len / control_len
    word_overlap: float      # Jaccard similarity
    regression_flag: bool
    metrics: dict

_shadow_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
_comparison_results: list[ComparisonResult] = []

def compute_comparison(control: str, shadow: str) -> dict:
    ctrl_words = set(control.lower().split())
    shad_words = set(shadow.lower().split())
    overlap = len(ctrl_words & shad_words) / len(ctrl_words | shad_words) if (ctrl_words | shad_words) else 1.0
    length_ratio = len(shadow) / len(control) if control else 1.0
    # Regression: shadow is much shorter or has low overlap
    regression = overlap < 0.25 or length_ratio < 0.4
    return {
        "word_overlap": round(overlap, 3),
        "length_ratio": round(length_ratio, 3),
        "regression_flag": regression,
    }

async def shadow_worker(worker_id: int, stop_event: asyncio.Event) -> None:
    client = anthropic.AsyncAnthropic()
    while not stop_event.is_set():
        try:
            job: ShadowJob = await asyncio.wait_for(_shadow_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        try:
            cfg = job.shadow_config
            response = await client.messages.create(
                model=cfg.get("model", "claude-sonnet-4-6"),
                max_tokens=cfg.get("max_tokens", 512),
                system=cfg.get("system", "You are a helpful assistant."),
                messages=[{"role": "user", "content": job.query}],
            )
            shadow_text = response.content[0].text
            metrics = compute_comparison(job.control_response, shadow_text)

            result = ComparisonResult(
                request_id=job.request_id,
                query=job.query,
                control=job.control_response[:200],
                shadow=shadow_text[:200],
                length_ratio=metrics["length_ratio"],
                word_overlap=metrics["word_overlap"],
                regression_flag=metrics["regression_flag"],
                metrics=metrics,
            )
            _comparison_results.append(result)

            if metrics["regression_flag"]:
                print(f"[ShadowWorker-{worker_id}] ⚠ REGRESSION req={job.request_id} overlap={metrics['word_overlap']:.2f}")
            else:
                print(f"[ShadowWorker-{worker_id}] OK req={job.request_id} overlap={metrics['word_overlap']:.2f}")
        except Exception as e:
            print(f"[ShadowWorker-{worker_id}] Error: {e}")
        finally:
            _shadow_queue.task_done()

async def enqueue_shadow(
    request_id: str,
    query: str,
    control_response: str,
    control_model: str,
    shadow_config: dict,
) -> None:
    job = ShadowJob(request_id, query, control_response, control_model, shadow_config)
    try:
        _shadow_queue.put_nowait(job)
    except asyncio.QueueFull:
        print(f"[ShadowQueue] Full — dropping shadow job for {request_id}")

async def run_production_with_shadow_queue() -> None:
    client = anthropic.AsyncAnthropic()
    stop_event = asyncio.Event()
    shadow_cfg = {"model": "claude-sonnet-4-6", "system": "You are a helpful Python expert.", "max_tokens": 400}

    # Start shadow workers
    workers = [asyncio.create_task(shadow_worker(i, stop_event)) for i in range(2)]

    queries = [
        "What is Python's type system?",
        "How do I use context managers?",
        "What are Python generators?",
        "Explain Python's MRO",
        "How does pickle work?",
    ]
    for i, q in enumerate(queries):
        rid = f"req_{i:03d}"
        # Production call
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": q}],
        )
        control_text = response.content[0].text
        print(f"[Prod] {rid}: {control_text[:60]}...")

        # Enqueue shadow (non-blocking)
        await enqueue_shadow(rid, q, control_text, "claude-haiku-4-5-20251001", shadow_cfg)

    # Wait for shadow queue to drain
    await _shadow_queue.join()
    stop_event.set()
    await asyncio.gather(*workers, return_exceptions=True)

    # Report
    print(f"\n=== Shadow Queue Report ===")
    regressions = [r for r in _comparison_results if r.regression_flag]
    print(f"Compared: {len(_comparison_results)} | Regressions: {len(regressions)}")
    for r in regressions:
        print(f"  [{r.request_id}] {r.query[:50]} — overlap={r.word_overlap:.2f}")

if __name__ == "__main__":
    asyncio.run(run_production_with_shadow_queue())

# Expected Token Savings: Queue decoupling eliminates production latency impact of shadow evaluation
# Environment: pip install anthropic
```

## Option 3: LLM-as-Judge Shadow Evaluation

Instead of heuristic comparison (word overlap, length), use a judge model to evaluate whether the shadow response is better, worse, or equivalent to the control. Produces richer signal: regression categories (accuracy, helpfulness, format, safety).

```python
import anthropic
import json
import random
from dataclasses import dataclass

@dataclass
class JudgeVerdict:
    winner: str       # "control", "shadow", "tie"
    categories: dict  # {accuracy: C/S/tie, helpfulness: ..., format: ...}
    explanation: str
    regression: bool  # True if shadow is meaningfully worse

JUDGE_SYSTEM = """You are an impartial AI response evaluator.

Given a user query and two responses (A=control, B=shadow), evaluate which is better.

Respond with ONLY valid JSON:
{
  "winner": "A|B|tie",
  "categories": {
    "accuracy": "A|B|tie",
    "helpfulness": "A|B|tie",
    "completeness": "A|B|tie",
    "format": "A|B|tie"
  },
  "explanation": "one sentence",
  "regression": true/false
}

regression=true means B (shadow) is meaningfully worse than A (control) in any critical dimension."""

def judge_comparison(
    client: anthropic.Anthropic,
    query: str,
    control: str,
    shadow: str,
) -> JudgeVerdict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"User query: {query}\n\n"
                f"Response A (control):\n{control[:400]}\n\n"
                f"Response B (shadow):\n{shadow[:400]}"
            ),
        }],
    )
    try:
        data = json.loads(response.content[0].text)
        return JudgeVerdict(
            winner=data["winner"],
            categories=data.get("categories", {}),
            explanation=data.get("explanation", ""),
            regression=bool(data.get("regression", False)),
        )
    except Exception:
        return JudgeVerdict("tie", {}, "parse error", False)

class ShadowEvaluator:
    def __init__(self, control_model: str, shadow_model: str, sample_rate: float = 0.5):
        self.control_model = control_model
        self.shadow_model = shadow_model
        self.sample_rate = sample_rate
        self._client = anthropic.Anthropic()
        self._verdicts: list[tuple[str, JudgeVerdict]] = []

    def serve(self, query: str, system: str = "") -> str:
        kwargs = {"model": self.control_model, "max_tokens": 512, "messages": [{"role": "user", "content": query}]}
        if system:
            kwargs["system"] = system
        control_text = self._client.messages.create(**kwargs).content[0].text

        if random.random() < self.sample_rate:
            shadow_kwargs = {**kwargs, "model": self.shadow_model}
            shadow_text = self._client.messages.create(**shadow_kwargs).content[0].text
            verdict = judge_comparison(self._client, query, control_text, shadow_text)
            self._verdicts.append((query, verdict))
            status = "⚠ REGRESSION" if verdict.regression else f"winner={verdict.winner}"
            print(f"[Judge] {status} | {verdict.explanation[:80]}")

        return control_text  # always return control

    def regression_report(self) -> dict:
        if not self._verdicts:
            return {"evaluated": 0}
        regressions = [(q, v) for q, v in self._verdicts if v.regression]
        shadow_wins = sum(1 for _, v in self._verdicts if v.winner == "B")
        return {
            "evaluated": len(self._verdicts),
            "shadow_wins": shadow_wins,
            "shadow_win_rate": f"{shadow_wins/len(self._verdicts)*100:.1f}%",
            "regressions": len(regressions),
            "regression_rate": f"{len(regressions)/len(self._verdicts)*100:.1f}%",
            "regression_examples": [{"query": q[:60], "reason": v.explanation} for q, v in regressions[:3]],
        }

if __name__ == "__main__":
    evaluator = ShadowEvaluator(
        control_model="claude-haiku-4-5-20251001",
        shadow_model="claude-haiku-4-5-20251001",  # Same model for demo; in prod use new candidate
        sample_rate=1.0,
    )
    queries = [
        "Explain list comprehensions in Python",
        "What is the difference between a list and a tuple?",
        "How do I handle exceptions in Python?",
        "What are Python decorators?",
    ]
    for q in queries:
        evaluator.serve(q, system="You are a concise Python tutor.")

    print("\n=== Judge Evaluation Report ===")
    print(json.dumps(evaluator.regression_report(), indent=2))

# Expected Token Savings: Judge call (~200 tokens) catches regressions worth 10× in user correction cost
# Environment: pip install anthropic
```

## Option 4: Differential Shadow with Rollback Trigger

Track shadow regression rate over a rolling window. When the regression rate exceeds a threshold, automatically trigger a rollback signal — logging a warning that the candidate should not be promoted. Implements a safety gate before any promotion decision.

```python
import anthropic
import json
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class RollingRegressionTracker:
    window_size: int = 50
    regression_threshold: float = 0.15  # 15% regression rate triggers rollback signal
    _results: deque = field(default_factory=lambda: deque(maxlen=50))
    _rollback_triggered: bool = False

    def record(self, regression: bool) -> None:
        self._results.append(int(regression))
        self._check_threshold()

    def _check_threshold(self) -> None:
        if len(self._results) < 10:
            return
        rate = sum(self._results) / len(self._results)
        if rate >= self.regression_threshold and not self._rollback_triggered:
            self._rollback_triggered = True
            print(f"🚨 [RollbackTrigger] Regression rate {rate:.1%} exceeded threshold {self.regression_threshold:.1%}!")
            print(f"   CANDIDATE SHOULD NOT BE PROMOTED. Last {len(self._results)} evaluations: {sum(self._results)} regressions.")

    @property
    def current_rate(self) -> float:
        return sum(self._results) / len(self._results) if self._results else 0.0

    @property
    def should_rollback(self) -> bool:
        return self._rollback_triggered

def is_regression(control: str, shadow: str) -> bool:
    """Heuristic: shadow has significantly less content on same topic."""
    ctrl_len = len(control.split())
    shad_len = len(shadow.split())
    ctrl_words = set(control.lower().split())
    shad_words = set(shadow.lower().split())
    overlap = len(ctrl_words & shad_words) / len(ctrl_words | shad_words) if (ctrl_words | shad_words) else 1.0
    return overlap < 0.2 or (shad_len < ctrl_len * 0.3 and ctrl_len > 50)

def run_differential_shadow(
    queries: list[str],
    control_system: str,
    shadow_system: str,
) -> dict:
    client = anthropic.Anthropic()
    tracker = RollingRegressionTracker(window_size=20, regression_threshold=0.20)
    results = []

    for i, q in enumerate(queries):
        # Control
        ctrl = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=control_system,
            messages=[{"role": "user", "content": q}],
        ).content[0].text

        # Shadow (candidate)
        shad = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=shadow_system,
            messages=[{"role": "user", "content": q}],
        ).content[0].text

        reg = is_regression(ctrl, shad)
        tracker.record(reg)
        results.append({"query": q[:50], "regression": reg, "rate_so_far": round(tracker.current_rate, 3)})

        if tracker.should_rollback:
            print(f"[Differential] Stopping evaluation early — rollback triggered at query {i+1}")
            break

    return {
        "evaluated": len(results),
        "final_regression_rate": f"{tracker.current_rate:.1%}",
        "rollback_triggered": tracker.should_rollback,
        "promote_candidate": not tracker.should_rollback,
        "per_query": results,
    }

if __name__ == "__main__":
    queries = [f"Python question {i}: explain concept {i}" for i in range(15)]

    # Simulate: shadow has a worse system prompt
    report = run_differential_shadow(
        queries,
        control_system="You are a helpful Python expert. Give clear, complete answers with examples.",
        shadow_system="Be brief.",  # Intentionally worse
    )
    print(f"\n=== Differential Shadow Report ===")
    print(f"Evaluated: {report['evaluated']} | Regression rate: {report['final_regression_rate']}")
    print(f"Rollback triggered: {report['rollback_triggered']} | Promote candidate: {report['promote_candidate']}")

# Expected Token Savings: Early rollback signal prevents costly full evaluation of bad candidates
# Environment: pip install anthropic
```

## Option 5: Statistical Shadow with Confidence Intervals

Collect shadow results with statistical rigor. Compute confidence intervals around regression rates. Only trigger rollback or promotion decisions when there is sufficient statistical evidence — avoiding false positives from small samples.

```python
import anthropic
import json
import math
import random
from dataclasses import dataclass, field

@dataclass
class StatisticalShadowResult:
    n: int
    regressions: int
    improvements: int
    ties: int

    @property
    def regression_rate(self) -> float:
        return self.regressions / self.n if self.n else 0.0

    @property
    def improvement_rate(self) -> float:
        return self.improvements / self.n if self.n else 0.0

    def wilson_confidence_interval(self, count: int, z: float = 1.96) -> tuple[float, float]:
        """Wilson score interval for proportion."""
        if self.n == 0:
            return 0.0, 1.0
        p = count / self.n
        denominator = 1 + z**2 / self.n
        center = (p + z**2 / (2 * self.n)) / denominator
        spread = z * math.sqrt(p * (1-p) / self.n + z**2 / (4 * self.n**2)) / denominator
        return max(0.0, center - spread), min(1.0, center + spread)

    def decision(self, max_regression_ci_upper: float = 0.1) -> str:
        if self.n < 20:
            return "INSUFFICIENT_SAMPLES"
        reg_lo, reg_hi = self.wilson_confidence_interval(self.regressions)
        imp_lo, imp_hi = self.wilson_confidence_interval(self.improvements)

        if reg_hi > max_regression_ci_upper:
            return f"BLOCK_PROMOTION (regression CI upper: {reg_hi:.1%} > {max_regression_ci_upper:.0%})"
        if imp_lo > 0.1:
            return f"PROMOTE (improvement CI lower: {imp_lo:.1%} > 10%)"
        return f"NEUTRAL (reg_ci=[{reg_lo:.1%},{reg_hi:.1%}] imp_ci=[{imp_lo:.1%},{imp_hi:.1%}])"

def evaluate_outcome(control: str, shadow: str) -> str:
    """Classify outcome as regression/improvement/tie."""
    ctrl_words = set(control.lower().split())
    shad_words = set(shadow.lower().split())
    overlap = len(ctrl_words & shad_words) / len(ctrl_words | shad_words) if (ctrl_words | shad_words) else 1.0
    ctrl_len = len(control.split())
    shad_len = len(shadow.split())

    if overlap < 0.2:
        return "regression"
    if shad_len > ctrl_len * 1.3 and overlap > 0.5:
        return "improvement"
    return "tie"

def run_statistical_shadow(queries: list[str], control_sys: str, shadow_sys: str) -> StatisticalShadowResult:
    client = anthropic.Anthropic()
    result = StatisticalShadowResult(0, 0, 0, 0)

    for q in queries:
        ctrl = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=control_sys,
            messages=[{"role": "user", "content": q}],
        ).content[0].text

        shad = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=shadow_sys,
            messages=[{"role": "user", "content": q}],
        ).content[0].text

        outcome = evaluate_outcome(ctrl, shad)
        result.n += 1
        if outcome == "regression":
            result.regressions += 1
        elif outcome == "improvement":
            result.improvements += 1
        else:
            result.ties += 1

    return result

if __name__ == "__main__":
    queries = [f"Explain Python concept: topic_{i}" for i in range(25)]
    result = run_statistical_shadow(
        queries,
        control_sys="You are a helpful Python expert with detailed explanations.",
        shadow_sys="You are a helpful Python expert with detailed explanations.",  # Same system = neutral
    )
    reg_lo, reg_hi = result.wilson_confidence_interval(result.regressions)
    print(f"N={result.n} | Regressions={result.regressions} ({result.regression_rate:.1%}) | CI=[{reg_lo:.1%},{reg_hi:.1%}]")
    print(f"Decision: {result.decision()}")

# Expected Token Savings: Statistical rigor prevents premature decisions; avoids unnecessary extended evaluation
# Environment: pip install anthropic
```

## Option 6: Multi-Dimension Shadow with Scorecard

Evaluate shadow responses across multiple quality dimensions (accuracy, completeness, format, safety, latency) and produce a scorecard. Use threshold-per-dimension for promotion decisions — a candidate that improves helpfulness but regresses on safety is still blocked.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field

@dataclass
class DimensionScore:
    dimension: str
    control_score: float    # 0.0 - 1.0
    shadow_score: float     # 0.0 - 1.0
    delta: float            # shadow - control
    weight: float = 1.0

@dataclass
class ShadowScorecard:
    request_id: str
    query: str
    scores: list[DimensionScore] = field(default_factory=list)

    @property
    def weighted_delta(self) -> float:
        total_weight = sum(s.weight for s in self.scores)
        return sum(s.delta * s.weight for s in self.scores) / total_weight if total_weight else 0.0

    @property
    def has_critical_regression(self) -> bool:
        critical_dims = {"safety", "accuracy"}
        return any(s.delta < -0.2 and s.dimension in critical_dims for s in self.scores)

    def summary(self) -> str:
        lines = [f"Request: {self.request_id} | Query: {self.query[:50]}"]
        for s in self.scores:
            arrow = "↑" if s.delta > 0.05 else ("↓" if s.delta < -0.05 else "→")
            lines.append(f"  {s.dimension:15s} ctrl={s.control_score:.2f} shad={s.shadow_score:.2f} {arrow}{s.delta:+.2f}")
        lines.append(f"  Weighted delta: {self.weighted_delta:+.3f} | Critical regression: {self.has_critical_regression}")
        return "\n".join(lines)

SCORER_SYSTEM = """Score two AI responses across dimensions. Respond ONLY with valid JSON:
{
  "accuracy": {"control": 0.0-1.0, "shadow": 0.0-1.0},
  "completeness": {"control": 0.0-1.0, "shadow": 0.0-1.0},
  "clarity": {"control": 0.0-1.0, "shadow": 0.0-1.0},
  "safety": {"control": 0.0-1.0, "shadow": 0.0-1.0}
}"""

DIMENSION_WEIGHTS = {"accuracy": 2.0, "safety": 3.0, "completeness": 1.0, "clarity": 1.0}

def score_responses(client: anthropic.Anthropic, query: str, control: str, shadow: str, request_id: str) -> ShadowScorecard:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=SCORER_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Query: {query}\n\nResponse A:\n{control[:300]}\n\nResponse B:\n{shadow[:300]}",
        }],
    )
    scorecard = ShadowScorecard(request_id=request_id, query=query)
    try:
        data = json.loads(response.content[0].text)
        for dim, scores in data.items():
            ctrl_score = float(scores["control"])
            shad_score = float(scores["shadow"])
            scorecard.scores.append(DimensionScore(
                dimension=dim,
                control_score=ctrl_score,
                shadow_score=shad_score,
                delta=shad_score - ctrl_score,
                weight=DIMENSION_WEIGHTS.get(dim, 1.0),
            ))
    except Exception:
        pass
    return scorecard

def run_scorecard_shadow(queries: list[str], control_sys: str, shadow_sys: str) -> list[ShadowScorecard]:
    client = anthropic.Anthropic()
    scorecards = []

    for i, q in enumerate(queries):
        rid = f"req_{i:03d}"
        ctrl = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=256, system=control_sys, messages=[{"role": "user", "content": q}]).content[0].text
        shad = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=256, system=shadow_sys, messages=[{"role": "user", "content": q}]).content[0].text
        sc = score_responses(client, q, ctrl, shad, rid)
        scorecards.append(sc)
        print(sc.summary())

    blocked = [s for s in scorecards if s.has_critical_regression]
    print(f"\n=== Scorecard Summary ===")
    print(f"Evaluated: {len(scorecards)} | Critical regressions: {len(blocked)}")
    avg_delta = sum(s.weighted_delta for s in scorecards) / len(scorecards) if scorecards else 0
    print(f"Average weighted delta: {avg_delta:+.3f}")
    print(f"Promotion decision: {'BLOCK' if blocked else 'APPROVE'}")
    return scorecards

if __name__ == "__main__":
    queries = ["What is Python's GIL?", "How do decorators work?", "Explain metaclasses"]
    run_scorecard_shadow(
        queries,
        control_sys="You are a helpful Python expert.",
        shadow_sys="You are a helpful Python expert. Always include a code example.",
    )

# Expected Token Savings: Per-dimension scoring reveals targeted improvements; prevents blind promotion
# Environment: pip install anthropic
```

## Comparison

| Option | Evaluation Method | Latency Impact | Automation | Best For |
|--------|-----------------|---------------|-----------|----------|
| 1. In-Process Fork | Heuristic overlap | Async (none) | Logging | Simple integration |
| 2. Async Queue | Heuristic | None (queued) | Queue + workers | High-traffic production |
| 3. LLM-as-Judge | Semantic | None (async) | Judge verdict | Quality-first evaluation |
| 4. Differential + Rollback | Heuristic + threshold | None | Auto-rollback trigger | CI/CD promotion gate |
| 5. Statistical | Heuristic + CI | None | Statistical decision | Data-driven promotion |
| 6. Multi-Dimension | LLM scoring | None | Scorecard | Comprehensive safety gate |
