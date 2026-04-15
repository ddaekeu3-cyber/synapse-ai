---
layout: solution
title: "Agent Doesn't Implement A/B Testing for Prompt Variants"
category: testing
description: "Agents ship prompt changes blind — no data on whether the new version is actually better. A/B testing routes a percentage of real traffic to each prompt variant, collects quality metrics, and lets you make evidence-based decisions about which prompt to promote to production."
tags: [ab-testing, prompt-engineering, experimentation, metrics, quality, evaluation, production]
---

# Agent Doesn't Implement A/B Testing for Prompt Variants

## Problem

Prompt engineering is trial and error without measurement. Teams rewrite system prompts based on anecdote — "this version feels better" — then ship to 100% of users with no rollback plan. When quality degrades, no one knows which change caused it. A/B testing provides a rigorous framework: split traffic between variants, measure real outcomes (user satisfaction, task completion, token cost, latency), and promote the winner with statistical confidence.

**Symptoms:**
- Prompt changes shipped without any quality measurement
- Regressions discovered days later via user complaints
- No way to compare two prompts on identical queries
- "Works on my test cases" deployed to production immediately
- Token costs increase after prompt changes go undetected

---

## Option 1: Simple Traffic Split with Metric Collection

```python
import anthropic
import random
import time
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ExperimentResult:
    result_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    experiment_id: str = ""
    variant: str = ""
    user_id: str = ""
    query: str = ""
    response: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    user_rating: Optional[int] = None  # 1-5, collected later
    timestamp: float = field(default_factory=time.time)

class ABTestStore:
    def __init__(self, db_path: str = "/tmp/ab_tests.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                result_id TEXT PRIMARY KEY,
                experiment_id TEXT,
                variant TEXT,
                user_id TEXT,
                query TEXT,
                response TEXT,
                latency_ms REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                user_rating INTEGER,
                timestamp REAL
            )
        """)
        self.db.commit()

    def record(self, result: ExperimentResult):
        self.db.execute(
            "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (result.result_id, result.experiment_id, result.variant,
             result.user_id, result.query, result.response,
             result.latency_ms, result.input_tokens, result.output_tokens,
             result.user_rating, result.timestamp)
        )
        self.db.commit()

    def rate(self, result_id: str, rating: int):
        self.db.execute(
            "UPDATE results SET user_rating = ? WHERE result_id = ?",
            (rating, result_id)
        )
        self.db.commit()

    def summary(self, experiment_id: str) -> dict:
        rows = self.db.execute("""
            SELECT variant,
                   COUNT(*) as n,
                   AVG(latency_ms) as avg_latency,
                   AVG(input_tokens + output_tokens) as avg_tokens,
                   AVG(user_rating) as avg_rating
            FROM results
            WHERE experiment_id = ?
            GROUP BY variant
        """, (experiment_id,)).fetchall()
        return {
            row[0]: {
                "n": row[1],
                "avg_latency_ms": round(row[2], 1),
                "avg_tokens": round(row[3], 1),
                "avg_rating": round(row[4], 2) if row[4] else None
            }
            for row in rows
        }

VARIANTS = {
    "control": "You are a helpful assistant. Answer the user's question clearly and concisely.",
    "treatment": (
        "You are a helpful assistant. Answer the user's question:\n"
        "1. Start with the direct answer\n"
        "2. Provide key supporting details\n"
        "3. End with a practical next step\n"
        "Keep your response under 150 words."
    )
}

def run_ab_experiment(
    experiment_id: str,
    user_id: str,
    query: str,
    store: ABTestStore,
    split: float = 0.5
) -> ExperimentResult:
    client = anthropic.Anthropic()
    variant = "treatment" if random.random() < split else "control"
    system_prompt = VARIANTS[variant]

    start = time.time()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": query}]
    )
    latency_ms = (time.time() - start) * 1000

    result = ExperimentResult(
        experiment_id=experiment_id,
        variant=variant,
        user_id=user_id,
        query=query,
        response=response.content[0].text,
        latency_ms=latency_ms,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens
    )
    store.record(result)
    return result

# Simulate an experiment
store = ABTestStore()
exp_id = "exp_structured_response_v1"

queries = [
    "How do I reverse a list in Python?",
    "What is the difference between TCP and UDP?",
    "How does garbage collection work in Java?",
]

results = []
for i, query in enumerate(queries * 3):  # 9 total calls
    user_id = f"user_{i}"
    result = run_ab_experiment(exp_id, user_id, query, store)
    # Simulate user rating (in production, collect from UI)
    simulated_rating = random.randint(3, 5) if result.variant == "treatment" else random.randint(2, 5)
    store.rate(result.result_id, simulated_rating)
    results.append(result)
    print(f"[{result.variant}] {query[:40]!r}: {result.latency_ms:.0f}ms, "
          f"{result.input_tokens + result.output_tokens} tokens, rating={simulated_rating}")

print(f"\nExperiment Summary ({exp_id}):")
for variant, stats in store.summary(exp_id).items():
    print(f"  {variant}: n={stats['n']}, "
          f"latency={stats['avg_latency_ms']}ms, "
          f"tokens={stats['avg_tokens']:.0f}, "
          f"rating={stats['avg_rating']}")

# Expected Token Savings: ~0% tracking overhead; treatment variant may save tokens via concise prompt
# Environment: SQLite for single-process; PostgreSQL for multi-instance production
```

---

## Option 2: Multi-Armed Bandit — Auto-Promote the Winning Variant

```python
import anthropic
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class BanditArm:
    name: str
    system_prompt: str
    successes: int = 0
    trials: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.trials if self.trials > 0 else 0.0

    def thompson_sample(self) -> float:
        """Thompson sampling: Beta(successes+1, failures+1)."""
        alpha = self.successes + 1
        beta = (self.trials - self.successes) + 1
        return random.betavariate(alpha, beta)

class PromptBandit:
    """
    Multi-armed bandit for prompt variants.
    Thompson sampling balances exploration vs exploitation:
    - Explore underperforming variants occasionally
    - Exploit the current best variant most of the time
    """

    def __init__(self, arms: list[BanditArm]):
        self.arms = {arm.name: arm for arm in arms}

    def select_arm(self) -> BanditArm:
        """Select arm via Thompson sampling."""
        scores = {name: arm.thompson_sample() for name, arm in self.arms.items()}
        winner = max(scores, key=scores.get)
        return self.arms[winner]

    def record_outcome(self, arm_name: str, success: bool):
        arm = self.arms[arm_name]
        arm.trials += 1
        if success:
            arm.successes += 1

    def stats(self) -> dict:
        return {
            name: {
                "trials": arm.trials,
                "success_rate": f"{arm.success_rate:.1%}",
                "thompson_estimate": f"{arm.thompson_sample():.3f}"
            }
            for name, arm in self.arms.items()
        }

def evaluate_response_quality(response_text: str, query: str) -> bool:
    """
    Simulate quality evaluation.
    In production: use LLM-as-judge, user thumbs up/down, or task completion check.
    """
    # Heuristic: treat as success if response is concise and contains key terms
    has_content = len(response_text.split()) > 10
    not_too_long = len(response_text.split()) < 200
    return has_content and not_too_long

def run_bandit_experiment(queries: list[str], rounds: int = 20):
    client = anthropic.Anthropic()

    bandit = PromptBandit([
        BanditArm(
            name="verbose",
            system_prompt="You are a helpful assistant. Be thorough and explain everything in detail."
        ),
        BanditArm(
            name="concise",
            system_prompt="You are a helpful assistant. Be direct. Give the answer first, then brief context. Max 3 sentences."
        ),
        BanditArm(
            name="structured",
            system_prompt="You are a helpful assistant. Format responses as: [Answer]: ... [Why]: ... [Example]: ..."
        )
    ])

    print(f"Running {rounds} rounds of Thompson sampling bandit:\n")
    arm_selections = defaultdict(int)

    for round_num in range(rounds):
        query = queries[round_num % len(queries)]
        arm = bandit.select_arm()
        arm_selections[arm.name] += 1

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=arm.system_prompt,
            messages=[{"role": "user", "content": query}]
        )

        success = evaluate_response_quality(response.content[0].text, query)
        bandit.record_outcome(arm.name, success)

        if round_num % 5 == 0:
            best = max(bandit.arms.values(), key=lambda a: a.success_rate)
            print(f"  Round {round_num:2d}: selected={arm.name}, success={success}, "
                  f"leading={best.name} ({best.success_rate:.0%})")

    print(f"\nFinal stats:")
    for name, stats in bandit.stats().items():
        print(f"  {name}: {stats['trials']} trials, "
              f"success_rate={stats['success_rate']}, "
              f"selections={arm_selections[name]}")

    winner = max(bandit.arms.values(), key=lambda a: a.success_rate)
    print(f"\nRecommended variant: {winner.name} ({winner.success_rate:.0%} success rate)")
    return winner.name

queries = [
    "Explain REST vs GraphQL",
    "What is a closure in JavaScript?",
    "How do I use Python decorators?",
]
run_bandit_experiment(queries, rounds=15)

# Expected Token Savings: ~15-25% — bandit naturally routes to the more concise winning variant
# Environment: In-memory bandit state; persist to Redis/DB for multi-process deployments
```

---

## Option 3: Holdout Testing with Statistical Significance Check

```python
import anthropic
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class VariantMetrics:
    variant: str
    n: int = 0
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    quality_scores: list[float] = field(default_factory=list)

    @property
    def avg_tokens(self) -> float:
        return self.total_tokens / self.n if self.n else 0.0

    @property
    def avg_latency(self) -> float:
        return self.total_latency_ms / self.n if self.n else 0.0

    @property
    def avg_quality(self) -> float:
        return sum(self.quality_scores) / len(self.quality_scores) if self.quality_scores else 0.0

def z_test_proportions(n_a: int, x_a: int, n_b: int, x_b: int) -> tuple[float, float]:
    """Two-proportion z-test for success rates. Returns (z_score, p_value_approx)."""
    p_a = x_a / n_a if n_a > 0 else 0
    p_b = x_b / n_b if n_b > 0 else 0
    p_pool = (x_a + x_b) / (n_a + n_b) if (n_a + n_b) > 0 else 0.5
    se = math.sqrt(p_pool * (1 - p_pool) * (1/n_a + 1/n_b)) if (n_a > 0 and n_b > 0) else 1.0
    z = (p_b - p_a) / se if se > 0 else 0.0
    # Approximate two-tailed p-value using normal CDF approximation
    p_approx = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, p_approx

def llm_quality_judge(client: anthropic.Anthropic, query: str, response: str) -> float:
    """Use LLM as quality judge. Returns score 0.0-1.0."""
    judge_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system="You are a quality evaluator. Rate the response from 1-10 (integer only).",
        messages=[{
            "role": "user",
            "content": f"Query: {query}\nResponse: {response}\n\nRating (1-10):"
        }]
    )
    text = judge_response.content[0].text.strip()
    try:
        score = int(''.join(c for c in text if c.isdigit())[:2])
        return min(max(score, 1), 10) / 10.0
    except (ValueError, IndexError):
        return 0.5

def run_holdout_experiment(
    queries: list[str],
    n_per_variant: int = 8,
    alpha: float = 0.05,
    use_llm_judge: bool = False
):
    client = anthropic.Anthropic()

    prompts = {
        "control": "You are a helpful assistant.",
        "treatment": "You are a concise technical assistant. Lead with the answer. Be specific. Avoid preamble."
    }

    metrics = {v: VariantMetrics(v) for v in prompts}
    results_by_variant: dict[str, list[dict]] = {v: [] for v in prompts}

    # Interleave queries across variants for fair comparison
    all_assignments = (["control"] * n_per_variant + ["treatment"] * n_per_variant)
    random.shuffle(all_assignments)

    query_iter = (queries * ((n_per_variant * 2 // len(queries)) + 1))[:n_per_variant * 2]

    for variant, query in zip(all_assignments, query_iter):
        start = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            system=prompts[variant],
            messages=[{"role": "user", "content": query}]
        )
        latency_ms = (time.time() - start) * 1000
        response_text = response.content[0].text

        tokens = response.usage.input_tokens + response.usage.output_tokens
        quality = llm_quality_judge(client, query, response_text) if use_llm_judge else random.uniform(0.5, 1.0)

        m = metrics[variant]
        m.n += 1
        m.total_tokens += tokens
        m.total_latency_ms += latency_ms
        m.quality_scores.append(quality)

        print(f"  [{variant}] tokens={tokens}, latency={latency_ms:.0f}ms, quality={quality:.2f}")

    # Statistical significance test
    ctrl = metrics["control"]
    trt = metrics["treatment"]

    # Define "success" as quality >= 0.7
    ctrl_successes = sum(1 for q in ctrl.quality_scores if q >= 0.7)
    trt_successes = sum(1 for q in trt.quality_scores if q >= 0.7)
    z, p_value = z_test_proportions(ctrl.n, ctrl_successes, trt.n, trt_successes)

    print(f"\n=== Experiment Results ===")
    for v, m in metrics.items():
        print(f"\n{v.upper()}:")
        print(f"  N={m.n}, avg_tokens={m.avg_tokens:.0f}, "
              f"avg_latency={m.avg_latency:.0f}ms, avg_quality={m.avg_quality:.2f}")

    print(f"\nStatistical Test (quality >= 0.7 success rate):")
    print(f"  Control:   {ctrl_successes}/{ctrl.n} ({ctrl_successes/ctrl.n:.0%})")
    print(f"  Treatment: {trt_successes}/{trt.n} ({trt_successes/trt.n:.0%})")
    print(f"  Z-score: {z:.3f}, p-value: {p_value:.4f}")
    print(f"  Significant at alpha={alpha}: {p_value < alpha}")

    if p_value < alpha:
        winner = "treatment" if trt.avg_quality > ctrl.avg_quality else "control"
        print(f"  -> PROMOTE: {winner}")
    else:
        print(f"  -> INCONCLUSIVE: run more traffic")

queries = [
    "What is dependency injection?",
    "How does async/await work in Python?",
    "Explain the CAP theorem",
]
run_holdout_experiment(queries, n_per_variant=6, use_llm_judge=False)

# Expected Token Savings: LLM judge adds ~10% overhead; worth it for reliable quality measurement
# Environment: Production: use Statsig/LaunchDarkly for traffic splitting; store results in Postgres
```

---

## Option 4: Prompt Variant Registry with Canary Rollout

```python
import anthropic
import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

class RolloutStage(Enum):
    SHADOW = "shadow"      # Run alongside production, don't serve to users
    CANARY = "canary"      # 5% of traffic
    RAMP = "ramp"          # 20-50% of traffic
    FULL = "full"          # 100% of traffic
    RETIRED = "retired"    # No longer used

@dataclass
class PromptVariant:
    variant_id: str
    name: str
    system_prompt: str
    stage: RolloutStage = RolloutStage.SHADOW
    traffic_pct: float = 0.0
    created_at: float = field(default_factory=time.time)
    metrics: dict = field(default_factory=lambda: {"calls": 0, "tokens": 0, "errors": 0})

    def advance_stage(self):
        transitions = {
            RolloutStage.SHADOW: (RolloutStage.CANARY, 0.05),
            RolloutStage.CANARY: (RolloutStage.RAMP, 0.25),
            RolloutStage.RAMP: (RolloutStage.FULL, 1.0),
            RolloutStage.FULL: (RolloutStage.FULL, 1.0),
        }
        next_stage, next_pct = transitions.get(self.stage, (self.stage, self.traffic_pct))
        self.stage = next_stage
        self.traffic_pct = next_pct
        print(f"[Rollout] {self.name}: {self.stage.value} ({self.traffic_pct:.0%} traffic)")

class VariantRegistry:
    def __init__(self):
        self._variants: dict[str, PromptVariant] = {}
        self._production: Optional[str] = None

    def register(self, variant: PromptVariant, is_production: bool = False):
        self._variants[variant.variant_id] = variant
        if is_production:
            self._production = variant.variant_id

    def select_for_request(self, user_id: str) -> PromptVariant:
        """Deterministic user assignment + traffic weighting."""
        # Hash user_id for sticky assignment (same user always gets same variant)
        user_hash = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % 100 / 100.0

        # Collect non-retired, non-shadow variants
        candidates = [
            v for v in self._variants.values()
            if v.stage not in (RolloutStage.SHADOW, RolloutStage.RETIRED)
        ]
        if not candidates:
            return self._variants[self._production]

        # Sort by variant_id for determinism; assign based on traffic bucket
        candidates.sort(key=lambda v: v.variant_id)
        cumulative = 0.0
        for variant in candidates:
            cumulative += variant.traffic_pct
            if user_hash < cumulative:
                return variant

        # Fallback to production
        return self._variants[self._production]

    def rollback(self, variant_id: str):
        v = self._variants.get(variant_id)
        if v:
            v.stage = RolloutStage.RETIRED
            v.traffic_pct = 0.0
            print(f"[Rollback] {v.name} retired")

    def status(self):
        for v in self._variants.values():
            print(f"  {v.name}: stage={v.stage.value}, "
                  f"traffic={v.traffic_pct:.0%}, "
                  f"calls={v.metrics['calls']}")

from typing import Optional

def run_canary_experiment():
    client = anthropic.Anthropic()

    # Set up registry
    registry = VariantRegistry()

    # Production (current)
    prod_variant = PromptVariant(
        variant_id="v1_prod",
        name="v1_production",
        system_prompt="You are a helpful assistant.",
        stage=RolloutStage.FULL,
        traffic_pct=1.0
    )
    registry.register(prod_variant, is_production=True)

    # New candidate — start in shadow
    new_variant = PromptVariant(
        variant_id="v2_candidate",
        name="v2_structured",
        system_prompt="You are a helpful assistant. Structure your response as: [Direct Answer] → [Brief Explanation] → [Example if needed]. Max 100 words.",
        stage=RolloutStage.SHADOW,
        traffic_pct=0.0
    )
    registry.register(new_variant)

    # Simulate staged rollout over time
    print("Initial state:")
    registry.status()

    # Shadow run — test but don't serve
    print("\n[Shadow] Testing new variant on 3 queries without serving to users...")
    test_queries = ["What is memoization?", "Explain REST APIs", "What is a hash map?"]
    for q in test_queries:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=new_variant.system_prompt,
            messages=[{"role": "user", "content": q}]
        )
        new_variant.metrics["calls"] += 1
        new_variant.metrics["tokens"] += response.usage.output_tokens
    print(f"Shadow: avg_tokens={new_variant.metrics['tokens']//3}")

    # Advance to canary
    new_variant.advance_stage()
    prod_variant.traffic_pct = 0.95  # Reduce production traffic

    # Simulate serving 10 users
    print("\n[Canary] Serving 10 users...")
    for i in range(10):
        user_id = f"user_{i:04d}"
        selected = registry.select_for_request(user_id)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=selected.system_prompt,
            messages=[{"role": "user", "content": "What is a binary search tree?"}]
        )
        selected.metrics["calls"] += 1
        print(f"  user_{i}: -> {selected.name} ({len(response.content[0].text.split())} words)")

    print("\nPost-canary state:")
    registry.status()

run_canary_experiment()

# Expected Token Savings: ~0% tracking overhead; canary limits blast radius of bad prompts
# Environment: Replace registry with feature flag service (LaunchDarkly, Statsig, GrowthBook)
```

---

## Option 5: Automated Prompt Tournament — Bracket-Style Elimination

```python
import anthropic
import random
import time
from dataclasses import dataclass, field

@dataclass
class Competitor:
    name: str
    system_prompt: str
    wins: int = 0
    losses: int = 0
    total_quality: float = 0.0
    matches: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.matches if self.matches > 0 else 0.0

def head_to_head(
    client: anthropic.Anthropic,
    a: Competitor,
    b: Competitor,
    query: str
) -> Competitor:
    """Run two variants on the same query and pick the better response via LLM judge."""

    def get_response(competitor: Competitor) -> str:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=competitor.system_prompt,
            messages=[{"role": "user", "content": query}]
        )
        return r.content[0].text

    resp_a = get_response(a)
    resp_b = get_response(b)

    # LLM judge picks the better response (blind to variant names)
    judge_prompt = f"""Compare these two responses to the question: "{query}"

Response A:
{resp_a}

Response B:
{resp_b}

Which response is better? Reply with ONLY "A" or "B" and one sentence why."""

    judgment = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": judge_prompt}]
    )
    verdict = judgment.content[0].text.strip()
    winner = a if verdict.upper().startswith("A") else b
    loser = b if winner == a else a

    winner.wins += 1
    loser.losses += 1
    winner.matches += 1
    loser.matches += 1

    print(f"  [{a.name} vs {b.name}] -> {winner.name} wins | {verdict[:60]}")
    return winner

def run_tournament(prompts: dict[str, str], test_queries: list[str]):
    client = anthropic.Anthropic()
    competitors = [Competitor(name=k, system_prompt=v) for k, v in prompts.items()]
    random.shuffle(competitors)

    print(f"Tournament: {len(competitors)} competitors, {len(test_queries)} test queries\n")

    round_num = 1
    while len(competitors) > 1:
        print(f"--- Round {round_num} ({len(competitors)} competitors) ---")
        next_round = []
        pairs = list(zip(competitors[::2], competitors[1::2]))
        # Handle odd number — give bye to last competitor
        if len(competitors) % 2 == 1:
            next_round.append(competitors[-1])
            print(f"  {competitors[-1].name}: bye")

        for a, b in pairs:
            query = random.choice(test_queries)
            winner = head_to_head(client, a, b, query)
            next_round.append(winner)

        competitors = next_round
        round_num += 1
        print()

    champion = competitors[0]
    print(f"=== CHAMPION: {champion.name} ===")
    print(f"  Win rate: {champion.win_rate:.0%} ({champion.wins}W/{champion.losses}L)")
    print(f"  System prompt: {champion.system_prompt[:80]}...")
    return champion

prompts = {
    "default": "You are a helpful assistant.",
    "concise": "Answer briefly and directly. Lead with the key point.",
    "structured": "Use this format: [Answer]: [Explanation]: [Example]:",
    "expert": "You are a senior software engineer. Give precise technical answers with examples.",
}

test_queries = [
    "What is a race condition?",
    "Explain database indexing",
    "What is the difference between stack and heap memory?",
    "How does JWT authentication work?",
]

run_tournament(prompts, test_queries)

# Expected Token Savings: ~0% — judgment calls add overhead; tournament finds best prompt objectively
# Environment: Run offline / nightly; not for real-time traffic routing
```

---

## Option 6: Continuous A/B Monitoring with Drift Detection

```python
import anthropic
import math
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from collections import deque

@dataclass
class WindowMetrics:
    """Sliding window metrics for drift detection."""
    window_size: int = 50
    quality_scores: deque = field(default_factory=lambda: deque(maxlen=50))
    token_counts: deque = field(default_factory=lambda: deque(maxlen=50))

    def add(self, quality: float, tokens: int):
        self.quality_scores.append(quality)
        self.token_counts.append(tokens)

    @property
    def avg_quality(self) -> float:
        return sum(self.quality_scores) / len(self.quality_scores) if self.quality_scores else 0.0

    @property
    def avg_tokens(self) -> float:
        return sum(self.token_counts) / len(self.token_counts) if self.token_counts else 0.0

    @property
    def quality_std(self) -> float:
        if len(self.quality_scores) < 2:
            return 0.0
        mean = self.avg_quality
        variance = sum((x - mean) ** 2 for x in self.quality_scores) / len(self.quality_scores)
        return math.sqrt(variance)

def detect_drift(
    baseline: WindowMetrics,
    current: WindowMetrics,
    threshold_sigma: float = 2.0
) -> tuple[bool, str]:
    """CUSUM-lite: flag if current quality drops > threshold_sigma below baseline."""
    if baseline.quality_std == 0:
        return False, "insufficient baseline data"
    z = (current.avg_quality - baseline.avg_quality) / baseline.quality_std
    drifted = z < -threshold_sigma
    direction = "degraded" if z < 0 else "improved"
    return drifted, f"z={z:.2f} ({direction}: {current.avg_quality:.2f} vs {baseline.avg_quality:.2f})"

def run_continuous_monitoring(n_requests: int = 40):
    client = anthropic.Anthropic()

    variants = {
        "control": {
            "prompt": "You are a helpful assistant.",
            "window": WindowMetrics()
        },
        "treatment": {
            "prompt": "You are a direct assistant. Answer in 2 sentences max.",
            "window": WindowMetrics()
        }
    }

    # Establish baseline in first 10 requests
    baselines = {k: WindowMetrics() for k in variants}
    baseline_phase = 10

    print(f"Running {n_requests} requests with continuous drift monitoring:\n")

    queries = [
        "What is polymorphism in OOP?",
        "How does DNS resolution work?",
        "What is eventual consistency?",
        "Explain pub/sub messaging",
    ]

    for i in range(n_requests):
        query = queries[i % len(queries)]
        variant_name = "control" if random.random() < 0.5 else "treatment"
        v = variants[variant_name]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=v["prompt"],
            messages=[{"role": "user", "content": query}]
        )

        tokens = response.usage.output_tokens
        # Simulate quality score (in production: LLM judge or user rating)
        # Inject simulated degradation after request 25 in treatment
        if variant_name == "treatment" and i > 25:
            quality = random.uniform(0.2, 0.5)  # Simulated drift
        else:
            quality = random.uniform(0.6, 1.0)

        v["window"].add(quality, tokens)

        # Build baseline from first N requests
        if i < baseline_phase:
            baselines[variant_name].add(quality, tokens)

        # Drift detection after baseline established
        if i >= baseline_phase and i % 5 == 0:
            for vname, vdata in variants.items():
                drifted, reason = detect_drift(baselines[vname], vdata["window"])
                status = "DRIFT DETECTED" if drifted else "OK"
                print(f"  [i={i:2d}] {vname}: {status} | {reason} | "
                      f"avg_tokens={vdata['window'].avg_tokens:.0f}")
                if drifted:
                    print(f"    -> ALERT: Consider pausing {vname} rollout!")

    print("\nFinal summary:")
    for vname, vdata in variants.items():
        print(f"  {vname}: avg_quality={vdata['window'].avg_quality:.2f}, "
              f"avg_tokens={vdata['window'].avg_tokens:.0f}")

run_continuous_monitoring(n_requests=30)

# Expected Token Savings: ~0% monitoring overhead; drift detection prevents shipping degraded prompts
# Environment: Production: use Prometheus/Grafana for metrics; PagerDuty for drift alerts
```

---

## Comparison

| Option | Traffic Split | Auto-Promote | Statistical Test | Judge | Best For |
|--------|-------------|-------------|----------------|-------|----------|
| Simple Traffic Split | Random 50/50 | No | No | User rating | Basic A/B with explicit user feedback |
| Multi-Armed Bandit | Thompson sampling | Yes | Implicit | Outcome | Live traffic, continuous optimization |
| Holdout + Z-Test | Random 50/50 | Manual | Z-test | LLM judge | Rigorous statistical significance |
| Canary Rollout | Staged 5%→25%→100% | Manual gate | No | Error rate | Risk-averse production deployments |
| Tournament | Round-robin | Yes (bracket) | LLM head-to-head | LLM judge | Comparing many variants offline |
| Continuous Monitoring | Any | Rollback trigger | CUSUM drift | Quality signal | Ongoing production health monitoring |

**Recommendation:** Use **Option 3** (holdout + z-test) for initial prompt experiments to ensure statistical rigor. Deploy winners via **Option 4** (canary rollout) to limit blast radius. Run **Option 6** (continuous monitoring) permanently in production to catch prompt regressions from model updates or distribution shift.
