---
title: "Agent Doesn't Implement Canary Testing for Prompt Changes"
description: "Deploying prompt changes to 100% of traffic at once risks regressions. Canary testing routes a small percentage of requests to the new prompt, measures quality metrics, and automatically rolls back or promotes based on results before full rollout."
difficulty: advanced
category: reliability
tags: [reliability, canary, deployment, prompt-management, a-b-testing, rollout, quality-gates]
---

## Problem

A prompt engineer updates the system prompt to improve output quality, deploys it, and unknowingly degrades performance for a subset of queries. By the time users report problems, thousands of requests have been affected. Canary testing routes 5-10% of traffic to the new prompt, compares outcomes against the baseline, and only proceeds with full rollout after the canary passes quality gates.

```python
# BAD: atomic prompt swap — all-or-nothing, no safety net
SYSTEM_PROMPT = "You are a helpful assistant."  # deployed to 100%

async def handle(prompt: str) -> str:
    return await call_model(system=SYSTEM_PROMPT, user=prompt)
# One bad deploy affects all users simultaneously
```

## Solution 1: Traffic-Split Canary with Percentage Routing

Route a configurable percentage of traffic to the canary prompt and track outcomes.

```python
import asyncio
import random
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = AsyncAnthropic()

@dataclass
class PromptVariant:
    name: str
    system_prompt: str
    traffic_weight: float  # 0.0 to 1.0

@dataclass
class VariantMetrics:
    requests: int = 0
    successes: int = 0
    errors: int = 0
    total_tokens: int = 0
    total_latency: float = 0.0
    quality_scores: list[float] = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> float:
        return (self.total_latency / max(self.requests, 1)) * 1000

    @property
    def error_rate(self) -> float:
        return self.errors / max(self.requests, 1)

    @property
    def avg_quality(self) -> float:
        return sum(self.quality_scores) / len(self.quality_scores) if self.quality_scores else 0.0

class CanaryRouter:
    def __init__(self, variants: list[PromptVariant]):
        assert abs(sum(v.traffic_weight for v in variants) - 1.0) < 0.01, "Weights must sum to 1.0"
        self._variants = variants
        self._metrics: dict[str, VariantMetrics] = {v.name: VariantMetrics() for v in variants}

    def select_variant(self) -> PromptVariant:
        r = random.random()
        cumulative = 0.0
        for variant in self._variants:
            cumulative += variant.traffic_weight
            if r < cumulative:
                return variant
        return self._variants[-1]

    def record(self, variant_name: str, success: bool, latency: float, tokens: int, quality: float | None = None):
        m = self._metrics[variant_name]
        m.requests += 1
        if success:
            m.successes += 1
        else:
            m.errors += 1
        m.total_latency += latency
        m.total_tokens += tokens
        if quality is not None:
            m.quality_scores.append(quality)

    def report(self) -> dict:
        return {
            name: {
                "requests": m.requests,
                "error_rate": round(m.error_rate, 4),
                "avg_latency_ms": round(m.avg_latency_ms, 1),
                "avg_quality": round(m.avg_quality, 3),
                "avg_tokens": round(m.total_tokens / max(m.requests, 1), 1),
            }
            for name, m in self._metrics.items()
        }

    def canary_is_safe(
        self,
        canary_name: str,
        baseline_name: str,
        min_samples: int = 10,
        max_error_rate_delta: float = 0.05,
        max_latency_regression_pct: float = 0.20,
    ) -> tuple[bool, str]:
        canary = self._metrics[canary_name]
        baseline = self._metrics[baseline_name]

        if canary.requests < min_samples:
            return False, f"Not enough samples: {canary.requests}/{min_samples}"

        error_delta = canary.error_rate - baseline.error_rate
        if error_delta > max_error_rate_delta:
            return False, f"Error rate regression: {error_delta:.1%}"

        if baseline.avg_latency_ms > 0:
            latency_delta = (canary.avg_latency_ms - baseline.avg_latency_ms) / baseline.avg_latency_ms
            if latency_delta > max_latency_regression_pct:
                return False, f"Latency regression: +{latency_delta:.1%}"

        return True, "Canary healthy"

# Define variants
BASELINE = PromptVariant(
    "baseline",
    "You are a helpful assistant. Answer questions clearly and concisely.",
    traffic_weight=0.9
)
CANARY = PromptVariant(
    "canary",
    "You are a helpful assistant. Answer questions clearly, concisely, and always include a practical example.",
    traffic_weight=0.1
)

router = CanaryRouter([BASELINE, CANARY])

async def canary_call(user_prompt: str) -> tuple[str, str]:
    variant = router.select_variant()
    start = time.perf_counter()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=variant.system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        output = response.content[0].text if response.content else ""
        latency = time.perf_counter() - start
        router.record(variant.name, True, latency, response.usage.output_tokens)
        return output, variant.name
    except Exception as e:
        latency = time.perf_counter() - start
        router.record(variant.name, False, latency, 0)
        raise

async def main():
    prompts = [f"What is concept {i}?" for i in range(15)]
    results = await asyncio.gather(*[canary_call(p) for p in prompts])

    variant_counts = defaultdict(int)
    for _, variant in results:
        variant_counts[variant] += 1

    print(f"Traffic distribution: {dict(variant_counts)}")
    print("\nMetrics by variant:")
    for name, metrics in router.report().items():
        print(f"  [{name}] {metrics}")

    safe, reason = router.canary_is_safe("canary", "baseline", min_samples=1)
    print(f"\nCanary safe to promote: {safe} — {reason}")

asyncio.run(main())
```

## Solution 2: Shadow Mode Canary (No User Impact)

Run the canary prompt in parallel without showing results to users — pure measurement.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class ShadowResult:
    baseline_output: str
    canary_output: str
    baseline_latency_ms: float
    canary_latency_ms: float
    baseline_tokens: int
    canary_tokens: int

async def shadow_call(
    user_prompt: str,
    baseline_system: str,
    canary_system: str,
    shadow_rate: float = 0.2  # run canary 20% of the time
) -> tuple[str, ShadowResult | None]:
    """
    Always returns baseline response to user.
    Optionally runs canary in background for measurement.
    """
    baseline_start = time.perf_counter()
    baseline_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=baseline_system,
        messages=[{"role": "user", "content": user_prompt}]
    )
    baseline_output = baseline_resp.content[0].text if baseline_resp.content else ""
    baseline_latency = (time.perf_counter() - baseline_start) * 1000

    # Shadow: run canary in background without blocking user response
    shadow_result: ShadowResult | None = None
    if __import__("random").random() < shadow_rate:
        canary_start = time.perf_counter()
        try:
            canary_resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=canary_system,
                messages=[{"role": "user", "content": user_prompt}]
            )
            canary_output = canary_resp.content[0].text if canary_resp.content else ""
            canary_latency = (time.perf_counter() - canary_start) * 1000

            shadow_result = ShadowResult(
                baseline_output=baseline_output,
                canary_output=canary_output,
                baseline_latency_ms=round(baseline_latency, 1),
                canary_latency_ms=round(canary_latency, 1),
                baseline_tokens=baseline_resp.usage.output_tokens,
                canary_tokens=canary_resp.usage.output_tokens,
            )
        except Exception:
            pass  # shadow failures never affect the user

    return baseline_output, shadow_result

async def main():
    baseline_system = "You are a helpful assistant."
    canary_system = "You are a helpful assistant. Always structure your response with a brief summary first."

    shadow_results = []
    for i in range(8):
        output, shadow = await shadow_call(
            f"Explain concept {i} briefly",
            baseline_system,
            canary_system,
            shadow_rate=0.6
        )
        if shadow:
            shadow_results.append(shadow)
            print(f"[Shadow {len(shadow_results)}] baseline={shadow.baseline_latency_ms:.0f}ms, canary={shadow.canary_latency_ms:.0f}ms, token delta={shadow.canary_tokens - shadow.baseline_tokens:+d}")

    if shadow_results:
        avg_token_delta = sum(s.canary_tokens - s.baseline_tokens for s in shadow_results) / len(shadow_results)
        avg_latency_delta = sum(s.canary_latency_ms - s.baseline_latency_ms for s in shadow_results) / len(shadow_results)
        print(f"\nSummary ({len(shadow_results)} shadow calls):")
        print(f"  Avg token delta: {avg_token_delta:+.1f}")
        print(f"  Avg latency delta: {avg_latency_delta:+.1f}ms")

asyncio.run(main())
```

## Solution 3: Quality-Gated Canary Promotion

Automatically promote or roll back the canary based on LLM-judged output quality.

```python
import asyncio
import json
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum

client = AsyncAnthropic()

class CanaryStatus(Enum):
    RUNNING = "running"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"

@dataclass
class QualityJudgment:
    baseline_score: float  # 0.0 to 1.0
    canary_score: float
    winner: str  # "baseline" | "canary" | "tie"
    reasoning: str

async def judge_quality(
    question: str,
    baseline_answer: str,
    canary_answer: str
) -> QualityJudgment:
    prompt = (
        f"Question: {question}\n\n"
        f"Answer A: {baseline_answer[:300]}\n\n"
        f"Answer B: {canary_answer[:300]}\n\n"
        f"Score both answers 0.0-1.0 for helpfulness, accuracy, and clarity. "
        f"Output JSON: {{\"a_score\": 0.0-1.0, \"b_score\": 0.0-1.0, \"winner\": \"A\"|\"B\"|\"tie\", \"reason\": \"brief\"}}"
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[start:end])
        winner_map = {"A": "baseline", "B": "canary", "tie": "tie"}
        return QualityJudgment(
            baseline_score=float(data.get("a_score", 0.5)),
            canary_score=float(data.get("b_score", 0.5)),
            winner=winner_map.get(data.get("winner", "tie"), "tie"),
            reasoning=data.get("reason", "")
        )
    except Exception:
        return QualityJudgment(0.5, 0.5, "tie", "judgment failed")

@dataclass
class QualityGatedCanary:
    baseline_prompt: str
    canary_prompt: str
    promotion_threshold: float = 0.6  # canary wins 60% of comparisons
    rollback_threshold: float = 0.3   # canary wins fewer than 30% → rollback
    min_samples: int = 5
    status: CanaryStatus = CanaryStatus.RUNNING
    canary_wins: int = 0
    baseline_wins: int = 0
    ties: int = 0

    @property
    def total_judged(self) -> int:
        return self.canary_wins + self.baseline_wins + self.ties

    @property
    def canary_win_rate(self) -> float:
        if self.total_judged == 0:
            return 0.5
        return self.canary_wins / self.total_judged

    def record_judgment(self, judgment: QualityJudgment):
        if judgment.winner == "canary":
            self.canary_wins += 1
        elif judgment.winner == "baseline":
            self.baseline_wins += 1
        else:
            self.ties += 1

        if self.total_judged >= self.min_samples:
            win_rate = self.canary_win_rate
            if win_rate >= self.promotion_threshold:
                self.status = CanaryStatus.PROMOTED
                print(f"[Canary] PROMOTED — win rate: {win_rate:.0%}")
            elif win_rate <= self.rollback_threshold:
                self.status = CanaryStatus.ROLLED_BACK
                print(f"[Canary] ROLLED BACK — win rate: {win_rate:.0%}")

async def run_quality_gated_canary(test_questions: list[str]) -> QualityGatedCanary:
    canary = QualityGatedCanary(
        baseline_prompt="You are a helpful assistant. Be clear and concise.",
        canary_prompt="You are a helpful assistant. Be clear, concise, and always give one specific example.",
    )

    for question in test_questions:
        if canary.status != CanaryStatus.RUNNING:
            break

        # Get both responses
        baseline_resp, canary_resp = await asyncio.gather(
            client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                system=canary.baseline_prompt,
                messages=[{"role": "user", "content": question}]
            ),
            client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                system=canary.canary_prompt,
                messages=[{"role": "user", "content": question}]
            )
        )

        baseline_text = baseline_resp.content[0].text if baseline_resp.content else ""
        canary_text = canary_resp.content[0].text if canary_resp.content else ""

        judgment = await judge_quality(question, baseline_text, canary_text)
        canary.record_judgment(judgment)
        print(f"[Judge] Q: {question[:40]}... Winner: {judgment.winner} (canary win rate: {canary.canary_win_rate:.0%})")

    return canary

async def main():
    questions = [
        "What is a REST API?",
        "How does caching work?",
        "What is a database index?",
        "What is async programming?",
        "What is a load balancer?",
    ]
    result = await run_quality_gated_canary(questions)
    print(f"\nFinal status: {result.status.value}")
    print(f"Canary wins: {result.canary_wins}, Baseline wins: {result.baseline_wins}, Ties: {result.ties}")

asyncio.run(main())
```

## Solution 4: Cohort-Based Canary (User Segment Targeting)

Route specific user cohorts (by ID hash) to the canary for deterministic assignment.

```python
import asyncio
import hashlib
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class CohortConfig:
    canary_prompt: str
    baseline_prompt: str
    canary_cohort_pct: int = 10  # 0-100

def get_user_cohort(user_id: str, salt: str = "canary-v2") -> int:
    """Returns a stable 0-99 bucket for the user. Same user always gets same bucket."""
    hash_input = f"{salt}:{user_id}"
    hash_val = int(hashlib.md5(hash_input.encode()).hexdigest(), 16)
    return hash_val % 100

def is_in_canary(user_id: str, canary_pct: int, salt: str = "canary-v2") -> bool:
    return get_user_cohort(user_id, salt) < canary_pct

async def cohort_routed_call(
    user_id: str,
    user_message: str,
    config: CohortConfig
) -> tuple[str, str]:
    in_canary = is_in_canary(user_id, config.canary_cohort_pct)
    system = config.canary_prompt if in_canary else config.baseline_prompt
    variant = "canary" if in_canary else "baseline"

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text if response.content else "", variant

async def main():
    config = CohortConfig(
        canary_prompt="You are a helpful assistant. Use bullet points for clarity.",
        baseline_prompt="You are a helpful assistant.",
        canary_cohort_pct=20
    )

    # Simulate 10 different users — same user always gets same variant
    user_ids = [f"user-{i:03d}" for i in range(10)]
    results = await asyncio.gather(*[
        cohort_routed_call(uid, "Explain what a database is.", config)
        for uid in user_ids
    ])

    from collections import Counter
    variant_counts = Counter(variant for _, variant in results)
    print(f"Cohort distribution: {dict(variant_counts)}")
    print(f"Expected ~{config.canary_cohort_pct}% canary, got {variant_counts.get('canary', 0)/len(results):.0%}")

    # Verify stability: same user always gets same bucket
    user = "user-042"
    bucket = get_user_cohort(user)
    print(f"\nUser {user}: bucket={bucket}, canary={bucket < config.canary_cohort_pct}")

asyncio.run(main())
```

## Solution 5: Automated Rollback on Error Spike

Monitor error rates in real time and automatically roll back the canary if errors spike.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import deque

client = AsyncAnthropic()

@dataclass
class ErrorWindow:
    window_seconds: float = 60.0
    events: deque = field(default_factory=lambda: deque())

    def record(self, is_error: bool):
        self.events.append((time.time(), is_error))
        self._evict()

    def _evict(self):
        cutoff = time.time() - self.window_seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    @property
    def error_rate(self) -> float:
        if not self.events:
            return 0.0
        errors = sum(1 for _, e in self.events if e)
        return errors / len(self.events)

    @property
    def sample_count(self) -> int:
        self._evict()
        return len(self.events)

class AutoRollbackCanary:
    def __init__(
        self,
        baseline_prompt: str,
        canary_prompt: str,
        canary_pct: float = 0.1,
        error_threshold: float = 0.15,
        min_samples_for_rollback: int = 5,
    ):
        self._baseline = baseline_prompt
        self._canary = canary_prompt
        self._canary_pct = canary_pct
        self._error_threshold = error_threshold
        self._min_samples = min_samples_for_rollback
        self._rolled_back = False
        self._canary_errors = ErrorWindow()
        self._baseline_errors = ErrorWindow()

    def _select(self) -> tuple[str, str]:
        import random
        if self._rolled_back or random.random() >= self._canary_pct:
            return self._baseline, "baseline"
        return self._canary, "canary"

    def _check_rollback(self):
        if self._rolled_back:
            return
        if self._canary_errors.sample_count < self._min_samples:
            return
        canary_rate = self._canary_errors.error_rate
        baseline_rate = self._baseline_errors.error_rate
        delta = canary_rate - baseline_rate
        if delta > self._error_threshold:
            self._rolled_back = True
            print(f"[AutoRollback] Triggered! canary_error_rate={canary_rate:.1%}, baseline={baseline_rate:.1%}, delta={delta:.1%}")

    async def call(self, user_message: str) -> str:
        prompt, variant = self._select()
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=prompt,
                messages=[{"role": "user", "content": user_message}]
            )
            output = response.content[0].text if response.content else ""
            if variant == "canary":
                self._canary_errors.record(False)
            else:
                self._baseline_errors.record(False)
            return output
        except Exception as e:
            if variant == "canary":
                self._canary_errors.record(True)
            else:
                self._baseline_errors.record(True)
            self._check_rollback()
            raise

async def main():
    canary = AutoRollbackCanary(
        baseline_prompt="You are a helpful assistant.",
        canary_prompt="You are a helpful assistant. Always respond in exactly 3 sentences.",
        canary_pct=0.4,
        error_threshold=0.1,
        min_samples_for_rollback=3,
    )

    prompts = [f"Question {i}?" for i in range(10)]
    results = await asyncio.gather(*[canary.call(p) for p in prompts], return_exceptions=True)
    successes = sum(1 for r in results if not isinstance(r, Exception))
    print(f"Completed: {successes}/{len(prompts)}, Rolled back: {canary._rolled_back}")

asyncio.run(main())
```

## Solution 6: Canary Metrics Dashboard

Collect and display comprehensive canary vs. baseline metrics for human review.

```python
import asyncio
import json
import random
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = AsyncAnthropic()

@dataclass
class CallRecord:
    variant: str
    latency_ms: float
    output_tokens: int
    success: bool
    timestamp: float = field(default_factory=time.time)

class CanaryDashboard:
    def __init__(self, baseline_prompt: str, canary_prompt: str, canary_pct: float = 0.15):
        self._prompts = {"baseline": baseline_prompt, "canary": canary_prompt}
        self._canary_pct = canary_pct
        self._records: dict[str, list[CallRecord]] = defaultdict(list)

    def _select_variant(self) -> str:
        return "canary" if random.random() < self._canary_pct else "baseline"

    async def call(self, user_message: str) -> str:
        variant = self._select_variant()
        start = time.perf_counter()
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=self._prompts[variant],
                messages=[{"role": "user", "content": user_message}]
            )
            output = response.content[0].text if response.content else ""
            self._records[variant].append(CallRecord(
                variant=variant,
                latency_ms=(time.perf_counter() - start) * 1000,
                output_tokens=response.usage.output_tokens,
                success=True
            ))
            return output
        except Exception:
            self._records[variant].append(CallRecord(
                variant=variant,
                latency_ms=(time.perf_counter() - start) * 1000,
                output_tokens=0, success=False
            ))
            raise

    def _stats(self, records: list[CallRecord]) -> dict:
        if not records:
            return {}
        latencies = sorted(r.latency_ms for r in records)
        n = len(latencies)
        return {
            "n": n,
            "success_rate": sum(1 for r in records if r.success) / n,
            "p50_ms": latencies[int(n * 0.50)],
            "p95_ms": latencies[int(n * 0.95)],
            "avg_tokens": sum(r.output_tokens for r in records) / n,
        }

    def render(self) -> str:
        lines = ["=== Canary Dashboard ==="]
        for variant in ["baseline", "canary"]:
            stats = self._stats(self._records[variant])
            if not stats:
                lines.append(f"\n[{variant.upper()}] No data")
                continue
            lines.append(f"\n[{variant.upper()}]")
            lines.append(f"  Requests:     {stats['n']}")
            lines.append(f"  Success rate: {stats['success_rate']:.1%}")
            lines.append(f"  p50 latency:  {stats['p50_ms']:.0f}ms")
            lines.append(f"  p95 latency:  {stats['p95_ms']:.0f}ms")
            lines.append(f"  Avg tokens:   {stats['avg_tokens']:.1f}")
        return "\n".join(lines)

async def main():
    dashboard = CanaryDashboard(
        baseline_prompt="You are a helpful assistant.",
        canary_prompt="You are a helpful assistant. Be especially concise.",
        canary_pct=0.3
    )
    prompts = [f"Explain briefly: {topic}" for topic in
               ["caching", "indexing", "sharding", "replication", "load balancing",
                "circuit breaker", "rate limiting", "connection pooling"]]
    await asyncio.gather(*[dashboard.call(p) for p in prompts])
    print(dashboard.render())

asyncio.run(main())
```

## Comparison

| Approach | User Impact | Measurement | Automation | Best For |
|---|---|---|---|---|
| Traffic-Split | Partial (canary %) | Metrics | Semi-auto | General canary deployments |
| Shadow Mode | Zero | Quality comparison | Manual review | High-risk changes |
| Quality-Gated | Partial | LLM-judged quality | Fully automated | Quality-first teams |
| Cohort-Based | Deterministic | Per-cohort metrics | Semi-auto | User-specific features |
| Auto-Rollback | Partial | Error rate | Fully automated | Production safety nets |
| Dashboard | Partial | Full metrics | Manual decision | Human-in-the-loop rollouts |

**Rule of thumb**: Start with shadow mode for high-risk changes (zero user impact). Use traffic-split + auto-rollback for normal prompt updates. Add quality-gated promotion when you have enough volume to get statistically significant quality judgments (50+ samples).
