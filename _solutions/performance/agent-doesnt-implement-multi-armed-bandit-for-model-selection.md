---
title: "Agent Doesn't Implement Multi-Armed Bandit for Model Selection"
description: "AI agents that use a fixed model or naive A/B testing for model selection leave performance and cost improvements on the table. Learn six multi-armed bandit strategies that continuously learn which model produces the best outcomes and automatically shift traffic toward winners."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-multi-armed-bandit-for-model-selection
tags: [bandit, model-selection, optimization, A/B-testing, exploration, performance]
symptoms:
  - "Switching models requires a manual decision and redeployment"
  - "No systematic way to evaluate whether a new model is actually better in production"
  - "A/B tests run for weeks because nobody defined a stopping criterion"
  - "The cheapest model that meets quality thresholds is never automatically discovered"
  - "Model selection is fixed at deployment time and never adapts to changing query distributions"
---

## The Problem

AI agent developers face a constant tradeoff: more capable models produce better outputs but cost more and take longer. The optimal model depends on the query type, user segment, and current traffic mix — and it changes as models are updated. Fixed model selection means you either over-spend on a powerful model for simple queries or under-deliver with a cheap model on complex ones.

Multi-armed bandit algorithms solve this by continuously exploring the model space (trying different options) while exploiting the best-performing option (directing most traffic there). Unlike static A/B tests, bandits adapt in real time and converge to the best model automatically.

```python
# ❌ Fixed model selection — no learning
model = "claude-opus-4-6"  # Set at deployment, never changes

# ✓ Bandit-driven model selection
bandit = EpsilonGreedyBandit(models=["claude-haiku-4-5", "claude-3-5-sonnet", "claude-opus-4-6"])
model = bandit.select()
response = await call_model(model, prompt)
reward = evaluate_response(response)
bandit.update(model, reward)  # Bandit learns and adjusts selection probabilities
```

---

## Solution 1: Epsilon-Greedy Bandit

The simplest bandit strategy: with probability ε, explore a random model; with probability 1-ε, exploit the current best performer. Simple, effective, and easy to reason about.

```python
import random
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArmStats:
    model_id: str
    pulls: int = 0
    total_reward: float = 0.0
    total_cost_usd: float = 0.0
    last_pulled: float = 0.0

    @property
    def mean_reward(self) -> float:
        return self.total_reward / self.pulls if self.pulls > 0 else 0.0

    @property
    def avg_cost(self) -> float:
        return self.total_cost_usd / self.pulls if self.pulls > 0 else 0.0


class EpsilonGreedyBandit:
    """
    ε-greedy bandit for model selection.
    Explores randomly with probability ε, exploits best model otherwise.
    ε decays over time as we gain confidence in our estimates.
    """

    def __init__(
        self,
        models: list[str],
        epsilon: float = 0.10,
        epsilon_decay: float = 0.9995,
        min_epsilon: float = 0.02,
    ):
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.min_epsilon = min_epsilon
        self._arms: dict[str, ArmStats] = {m: ArmStats(model_id=m) for m in models}
        self._total_pulls = 0
        self._history: list[dict] = []

    def select(self) -> str:
        """Select a model to use."""
        self._total_pulls += 1
        # Decay epsilon
        self.epsilon = max(self.min_epsilon, self.epsilon * self.epsilon_decay)

        if random.random() < self.epsilon:
            # Explore: pick randomly
            return random.choice(list(self._arms.keys()))
        else:
            # Exploit: pick best known model
            return max(self._arms.values(), key=lambda a: a.mean_reward).model_id

    def update(self, model_id: str, reward: float, cost_usd: float = 0.0):
        """Update arm statistics after observing a reward."""
        arm = self._arms.get(model_id)
        if not arm:
            return
        arm.pulls += 1
        arm.total_reward += reward
        arm.total_cost_usd += cost_usd
        arm.last_pulled = time.time()
        self._history.append({
            "model": model_id,
            "reward": reward,
            "cost": cost_usd,
            "timestamp": time.time(),
            "epsilon": self.epsilon,
        })

    def best_model(self) -> str:
        return max(self._arms.values(), key=lambda a: a.mean_reward).model_id

    def stats(self) -> dict:
        total_pulls = sum(a.pulls for a in self._arms.values())
        return {
            "epsilon": self.epsilon,
            "total_pulls": total_pulls,
            "arms": {
                model_id: {
                    "pulls": arm.pulls,
                    "pull_pct": arm.pulls / max(total_pulls, 1) * 100,
                    "mean_reward": arm.mean_reward,
                    "avg_cost_usd": arm.avg_cost,
                }
                for model_id, arm in self._arms.items()
            },
            "current_best": self.best_model(),
        }
```

---

## Solution 2: Thompson Sampling (Bayesian Bandit)

Thompson sampling maintains a Beta distribution over each model's success probability. It draws a sample from each distribution and selects the model whose sample is highest — naturally balancing exploration and exploitation.

```python
import random
import math
from dataclasses import dataclass


@dataclass
class BetaArm:
    model_id: str
    alpha: float = 1.0  # Prior: 1 success
    beta: float = 1.0   # Prior: 1 failure
    pulls: int = 0
    total_cost: float = 0.0

    def sample(self) -> float:
        """Sample from Beta(alpha, beta) distribution."""
        return random.betavariate(self.alpha, self.beta)

    @property
    def mean_reward(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def confidence(self) -> float:
        """Higher = more confident in the estimate."""
        n = self.alpha + self.beta - 2  # Subtract priors
        return 1.0 - 1.0 / max(n + 1, 1)


class ThompsonSamplingBandit:
    """
    Bayesian bandit using Thompson Sampling.
    Converges faster than ε-greedy; naturally reduces exploration as confidence grows.
    Reward should be normalized to [0, 1].
    """

    def __init__(self, models: list[str], prior_alpha: float = 1.0, prior_beta: float = 1.0):
        self._arms: dict[str, BetaArm] = {
            m: BetaArm(model_id=m, alpha=prior_alpha, beta=prior_beta)
            for m in models
        }

    def select(self) -> str:
        """Sample from each arm's posterior and select the highest."""
        samples = {model_id: arm.sample() for model_id, arm in self._arms.items()}
        return max(samples, key=samples.__getitem__)

    def update(self, model_id: str, reward: float, cost_usd: float = 0.0):
        """
        Update posterior with new observation.
        reward: float in [0, 1]. Treated as probability of success.
        """
        arm = self._arms.get(model_id)
        if not arm:
            return
        # For continuous rewards, binarize using threshold or use directly
        arm.alpha += reward          # Success weight
        arm.beta += (1.0 - reward)   # Failure weight
        arm.pulls += 1
        arm.total_cost += cost_usd

    def add_arm(self, model_id: str):
        """Add a new model to explore."""
        if model_id not in self._arms:
            self._arms[model_id] = BetaArm(model_id=model_id)

    def remove_arm(self, model_id: str):
        """Remove a model from consideration."""
        self._arms.pop(model_id, None)

    def posterior_summary(self) -> dict:
        return {
            model_id: {
                "alpha": arm.alpha,
                "beta": arm.beta,
                "mean_reward": arm.mean_reward,
                "confidence": arm.confidence,
                "pulls": arm.pulls,
                "avg_cost": arm.total_cost / max(arm.pulls, 1),
            }
            for model_id, arm in self._arms.items()
        }

    def best_model(self) -> str:
        return max(self._arms.values(), key=lambda a: a.mean_reward).model_id

    def confident_recommendation(self, min_confidence: float = 0.90) -> str | None:
        """Return best model only if we're confident enough; else None (still exploring)."""
        best = max(self._arms.values(), key=lambda a: a.mean_reward)
        return best.model_id if best.confidence >= min_confidence else None
```

---

## Solution 3: UCB1 (Upper Confidence Bound) Bandit

UCB1 selects the model with the highest upper confidence bound, naturally exploring under-tried models while exploiting well-performing ones. Provides stronger theoretical guarantees than ε-greedy.

```python
import math
import time
from dataclasses import dataclass


@dataclass
class UCBArm:
    model_id: str
    pulls: int = 0
    total_reward: float = 0.0
    total_cost: float = 0.0

    @property
    def mean_reward(self) -> float:
        return self.total_reward / self.pulls if self.pulls > 0 else 0.0

    def ucb_score(self, total_pulls: int, confidence: float = 2.0) -> float:
        """UCB1 score: mean + sqrt(confidence * ln(N) / n)"""
        if self.pulls == 0:
            return float("inf")  # Unpulled arms are always tried first
        exploration_bonus = math.sqrt(confidence * math.log(total_pulls) / self.pulls)
        return self.mean_reward + exploration_bonus


class UCB1Bandit:
    """
    Upper Confidence Bound bandit for model selection.
    Theoretically optimal exploration: pulls unpulled arms first,
    then balances based on confidence intervals.
    """

    def __init__(self, models: list[str], confidence: float = 2.0):
        self.confidence = confidence
        self._arms: dict[str, UCBArm] = {m: UCBArm(model_id=m) for m in models}
        self._total_pulls = 0
        self._regret_tracker: list[float] = []

    def select(self) -> str:
        self._total_pulls += 1
        best_model = max(
            self._arms.values(),
            key=lambda a: a.ucb_score(self._total_pulls, self.confidence)
        )
        return best_model.model_id

    def update(self, model_id: str, reward: float, cost_usd: float = 0.0):
        arm = self._arms.get(model_id)
        if not arm:
            return
        arm.pulls += 1
        arm.total_reward += reward
        arm.total_cost += cost_usd
        # Track theoretical regret (difference from best known)
        best_mean = max(a.mean_reward for a in self._arms.values())
        self._regret_tracker.append(best_mean - reward)

    def cumulative_regret(self) -> float:
        return sum(self._regret_tracker)

    def ucb_scores(self) -> dict[str, float]:
        return {
            mid: arm.ucb_score(max(self._total_pulls, 1), self.confidence)
            for mid, arm in self._arms.items()
        }

    def stats(self) -> dict:
        scores = self.ucb_scores()
        return {
            "total_pulls": self._total_pulls,
            "cumulative_regret": self.cumulative_regret(),
            "arms": {
                mid: {
                    "pulls": arm.pulls,
                    "mean_reward": arm.mean_reward,
                    "ucb_score": scores[mid],
                    "avg_cost": arm.total_cost / max(arm.pulls, 1),
                }
                for mid, arm in self._arms.items()
            },
        }
```

---

## Solution 4: Contextual Bandit — Query-Type-Aware Model Selection

Different models perform differently on different query types. A contextual bandit learns separate policies per context (query complexity, domain, length) — routing simple queries to cheap models and complex ones to powerful models.

```python
import re
import math
from dataclasses import dataclass, field
from typing import Any
from collections import defaultdict


def extract_context(query: str) -> str:
    """
    Extract a context bucket from a query.
    In production, use an ML classifier. Here: simple heuristic.
    """
    length = len(query)
    has_code = bool(re.search(r'```|def |class |import |function ', query))
    has_math = bool(re.search(r'\d+[\+\-\*/]\d+|equation|formula|calculate', query))
    word_count = len(query.split())

    if has_code:
        return "coding"
    elif has_math:
        return "math"
    elif word_count < 20:
        return "simple"
    elif word_count > 200:
        return "complex"
    else:
        return "general"


@dataclass
class ContextualArm:
    model_id: str
    context: str
    alpha: float = 1.0
    beta: float = 1.0
    pulls: int = 0

    @property
    def mean_reward(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def sample(self) -> float:
        import random
        return random.betavariate(self.alpha, self.beta)


class ContextualBandit:
    """
    Bandit with separate per-context policies.
    Routes coding queries to models with strong coding performance,
    simple queries to cheap fast models, etc.
    """

    def __init__(self, models: list[str], contexts: list[str] | None = None):
        self.models = models
        self.contexts = contexts or ["simple", "general", "complex", "coding", "math"]
        # Per-context Thompson sampling arms
        self._arms: dict[str, dict[str, ContextualArm]] = {
            ctx: {m: ContextualArm(model_id=m, context=ctx) for m in models}
            for ctx in self.contexts
        }
        self._pulls_by_context: dict[str, int] = defaultdict(int)

    def select(self, query: str) -> tuple[str, str]:
        """Returns (model_id, context)."""
        context = extract_context(query)
        if context not in self._arms:
            context = "general"

        # Thompson sampling within this context
        context_arms = self._arms[context]
        samples = {mid: arm.sample() for mid, arm in context_arms.items()}
        selected = max(samples, key=samples.__getitem__)
        self._pulls_by_context[context] += 1
        return selected, context

    def update(self, model_id: str, context: str, reward: float):
        context_arms = self._arms.get(context, self._arms.get("general", {}))
        arm = context_arms.get(model_id)
        if arm:
            arm.alpha += reward
            arm.beta += (1.0 - reward)
            arm.pulls += 1

    def best_model_for_context(self, context: str) -> str:
        arms = self._arms.get(context, self._arms.get("general", {}))
        if not arms:
            return self.models[0]
        return max(arms.values(), key=lambda a: a.mean_reward).model_id

    def policy_summary(self) -> dict:
        return {
            ctx: {
                "best_model": self.best_model_for_context(ctx),
                "total_pulls": self._pulls_by_context[ctx],
                "arms": {
                    mid: {
                        "mean_reward": arm.mean_reward,
                        "pulls": arm.pulls,
                    }
                    for mid, arm in arms.items()
                },
            }
            for ctx, arms in self._arms.items()
        }
```

---

## Solution 5: Cost-Adjusted Bandit (Reward-Per-Dollar)

Optimize for quality per dollar rather than raw quality. A model that scores 0.85 quality at $0.001 per call beats a model scoring 0.90 at $0.01 per call if cost-adjusted quality is the objective.

```python
from dataclasses import dataclass, field
import random
import time


MODEL_COSTS = {
    "claude-haiku-4-5-20251001": 0.00025 / 1000,    # per output token
    "claude-3-5-sonnet-20241022": 0.003 / 1000,
    "claude-opus-4-6": 0.015 / 1000,
}


@dataclass
class CostAdjustedArm:
    model_id: str
    alpha: float = 1.0
    beta: float = 1.0
    pulls: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0

    @property
    def mean_quality(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def avg_cost_per_call(self) -> float:
        return self.total_cost / max(self.pulls, 1)

    def cost_adjusted_score(self, cost_weight: float = 0.3) -> float:
        """
        Combined score: (1-w) * quality - w * normalized_cost
        Higher is better.
        """
        if self.pulls == 0:
            return float("inf")  # Unexplored arms
        # Normalize cost to [0,1] range (relative to most expensive model)
        max_cost = max(MODEL_COSTS.values())
        cost_per_token = MODEL_COSTS.get(self.model_id, max_cost)
        normalized_cost = cost_per_token / max_cost
        return (1 - cost_weight) * self.mean_quality - cost_weight * normalized_cost

    def sample_cost_adjusted(self, cost_weight: float = 0.3) -> float:
        """Thompson sample with cost adjustment."""
        quality_sample = random.betavariate(self.alpha, self.beta)
        max_cost = max(MODEL_COSTS.values())
        cost_per_token = MODEL_COSTS.get(self.model_id, max_cost)
        normalized_cost = cost_per_token / max_cost
        return (1 - cost_weight) * quality_sample - cost_weight * normalized_cost


class CostAdjustedBandit:
    """
    Bandit that optimizes quality-per-dollar rather than raw quality.
    Automatically discovers the cheapest model that meets quality thresholds.
    """

    def __init__(
        self,
        models: list[str],
        cost_weight: float = 0.3,
        min_quality_threshold: float = 0.70,
    ):
        self.cost_weight = cost_weight
        self.min_quality = min_quality_threshold
        self._arms: dict[str, CostAdjustedArm] = {
            m: CostAdjustedArm(model_id=m) for m in models
        }

    def select(self) -> str:
        # Filter arms that clearly don't meet quality threshold
        viable = {
            mid: arm for mid, arm in self._arms.items()
            if arm.pulls < 5 or arm.mean_quality >= self.min_quality
        }
        if not viable:
            viable = self._arms  # All are failing — keep exploring

        samples = {mid: arm.sample_cost_adjusted(self.cost_weight) for mid, arm in viable.items()}
        return max(samples, key=samples.__getitem__)

    def update(self, model_id: str, quality_score: float,
               output_tokens: int, actual_cost_usd: float):
        arm = self._arms.get(model_id)
        if not arm:
            return
        arm.alpha += quality_score
        arm.beta += (1.0 - quality_score)
        arm.pulls += 1
        arm.total_cost += actual_cost_usd
        arm.total_tokens += output_tokens

    def efficiency_report(self) -> dict:
        return {
            model_id: {
                "mean_quality": arm.mean_quality,
                "avg_cost_per_call": arm.avg_cost_per_call,
                "cost_adjusted_score": arm.cost_adjusted_score(self.cost_weight),
                "pulls": arm.pulls,
                "meets_quality_threshold": arm.mean_quality >= self.min_quality,
            }
            for model_id, arm in self._arms.items()
        }

    def recommended_model(self) -> str:
        """The model with best cost-adjusted score that meets quality threshold."""
        qualifying = {
            mid: arm for mid, arm in self._arms.items()
            if arm.pulls >= 10 and arm.mean_quality >= self.min_quality
        }
        if not qualifying:
            return min(self._arms.keys())  # Default to cheapest while learning
        return max(qualifying.values(), key=lambda a: a.cost_adjusted_score(self.cost_weight)).model_id
```

---

## Solution 6: Bandit Orchestrator with Auto-Promotion and Gradual Rollout

A production-grade bandit orchestrator that wraps all bandit variants, handles reward signal collection, auto-promotes winners to higher traffic share, and provides a Prometheus metrics endpoint.

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
from typing import Callable, Any


@dataclass
class BanditCallResult:
    model_id: str
    context: str
    response: str
    quality_score: float
    cost_usd: float
    latency_ms: float
    output_tokens: int


class BanditOrchestrator:
    """
    Production bandit orchestrator.
    Wraps model calls with automatic reward scoring and bandit updates.
    Supports pluggable reward functions (LLM judge, rule-based, user feedback).
    """

    def __init__(
        self,
        models: list[str],
        strategy: str = "thompson",   # "epsilon_greedy", "ucb1", "thompson", "contextual", "cost"
        reward_fn: Callable | None = None,
        cost_weight: float = 0.3,
    ):
        self._bandit = self._init_bandit(models, strategy, cost_weight)
        self._reward_fn = reward_fn or self._default_reward
        self._client = anthropic.AsyncAnthropic()
        self._call_log: list[BanditCallResult] = []
        self._model_prices = MODEL_COSTS

    def _init_bandit(self, models, strategy, cost_weight):
        if strategy == "epsilon_greedy":
            return EpsilonGreedyBandit(models)
        elif strategy == "ucb1":
            return UCB1Bandit(models)
        elif strategy == "contextual":
            return ContextualBandit(models)
        elif strategy == "cost":
            return CostAdjustedBandit(models, cost_weight=cost_weight)
        else:
            return ThompsonSamplingBandit(models)

    async def call(
        self, messages: list[dict], system: str = "", max_tokens: int = 1024
    ) -> BanditCallResult:
        # Select model
        query = messages[-1].get("content", "") if messages else ""
        if hasattr(self._bandit, "select") and "contextual" in type(self._bandit).__name__.lower():
            model_id, context = self._bandit.select(query)
        else:
            model_id = self._bandit.select() if not isinstance(self._bandit.select(), tuple) else self._bandit.select()[0]
            context = "general"

        # Execute call
        start = time.monotonic()
        kwargs = {"model": model_id, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system

        resp = await self._client.messages.create(**kwargs)
        latency_ms = (time.monotonic() - start) * 1000

        response_text = resp.content[0].text
        output_tokens = resp.usage.output_tokens
        cost = self._model_prices.get(model_id, 0.003 / 1000) * output_tokens

        # Score the response
        quality = await self._reward_fn(query, response_text)

        # Update bandit
        if hasattr(self._bandit, "update"):
            if isinstance(self._bandit, CostAdjustedBandit):
                self._bandit.update(model_id, quality, output_tokens, cost)
            elif isinstance(self._bandit, ContextualBandit):
                self._bandit.update(model_id, context, quality)
            else:
                self._bandit.update(model_id, quality, cost)

        result = BanditCallResult(
            model_id=model_id, context=context, response=response_text,
            quality_score=quality, cost_usd=cost,
            latency_ms=latency_ms, output_tokens=output_tokens,
        )
        self._call_log.append(result)
        return result

    async def _default_reward(self, query: str, response: str) -> float:
        """Default reward: length-normalized quality heuristic. Replace with real scorer."""
        if not response:
            return 0.0
        length_score = min(len(response) / 500, 1.0)  # Prefer substantive responses
        return 0.5 + 0.5 * length_score

    def record_user_feedback(self, thumbs_up: bool):
        """Update bandit with explicit user feedback."""
        if self._call_log:
            last = self._call_log[-1]
            reward = 1.0 if thumbs_up else 0.0
            if isinstance(self._bandit, ContextualBandit):
                self._bandit.update(last.model_id, last.context, reward)
            else:
                self._bandit.update(last.model_id, reward)

    def prometheus_metrics(self) -> str:
        lines = []
        recent = self._call_log[-100:]
        if not recent:
            return ""
        for model_id in {r.model_id for r in recent}:
            model_calls = [r for r in recent if r.model_id == model_id]
            lbl = f'model="{model_id}"'
            avg_quality = sum(r.quality_score for r in model_calls) / len(model_calls)
            avg_latency = sum(r.latency_ms for r in model_calls) / len(model_calls)
            lines += [
                f'bandit_calls_total{{{lbl}}} {len(model_calls)}',
                f'bandit_avg_quality{{{lbl}}} {avg_quality:.4f}',
                f'bandit_avg_latency_ms{{{lbl}}} {avg_latency:.1f}',
            ]
        return "\n".join(lines)
```

---

## Comparison

| Strategy | Convergence Speed | Theoretical Optimality | Contextual | Best For |
|---|---|---|---|---|
| Epsilon-greedy | Medium | Low | No | Simple baseline, easy to debug |
| Thompson sampling | Fast | High (Bayesian optimal) | No | Most production use cases |
| UCB1 | Medium | High (regret bounded) | No | When theoretical guarantees matter |
| Contextual bandit | Fastest per context | High | Yes | Mixed workloads with distinct query types |
| Cost-adjusted | Medium | N/A (cost-quality tradeoff) | No | Cost optimization with quality floor |
| Bandit orchestrator | Depends on strategy | Depends on strategy | Optional | Production deployment with feedback loop |

**Recommendations:**
- Start with **Thompson sampling** (Solution 2) — it's fast, theoretically strong, and requires no hyperparameter tuning.
- Switch to **contextual bandit** (Solution 4) once you have enough traffic to distinguish query types — it typically reduces cost 30-50% by routing simple queries to cheaper models.
- Use **cost-adjusted bandit** (Solution 5) when you have a quality floor (e.g., "answers must be ≥ 70% correct") and want to minimize cost within that floor.
- Always instrument with **reward signals** before deploying any bandit — without feedback, all strategies degrade to random selection.
- Use the **orchestrator** (Solution 6) in production to get automatic reward collection, Prometheus metrics, and user feedback integration in one class.
