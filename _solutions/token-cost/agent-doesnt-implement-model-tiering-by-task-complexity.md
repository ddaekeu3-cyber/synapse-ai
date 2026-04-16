---
title: "Agent Doesn't Implement Model Tiering by Task Complexity"
description: "AI agents use a single model for all tasks — routing a simple factual lookup through the same expensive model as a complex multi-step analysis. Model tiering matches task complexity to model capability, reducing cost by 5–20x for routine work without degrading quality."
problem_description: |
  When an agent uses claude-opus-4-6 to answer "what does this variable name mean?" and the same model to architect a distributed system, it pays Opus pricing for every request regardless of actual complexity. Simple classification, entity extraction, short summaries, and yes/no questions can be handled accurately by claude-haiku-4-5-20251001 at ~1/10th the cost. Moderate tasks — code generation, multi-paragraph explanations, tool call planning — fit claude-sonnet-4-6. Only genuinely complex tasks — novel reasoning, long-form synthesis, adversarial analysis — justify Opus. A tiering layer classifies each request and routes it to the minimum-cost model that meets the quality bar.
category: token-cost
difficulty: intermediate
tags: [model-tiering, cost-optimization, model-routing, task-classification, efficiency]
---

## Solution 1: Rule-Based Complexity Router

Classify requests using keyword rules and heuristics — fast, zero-cost routing that handles the majority of clear-cut cases without an additional model call.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    FAST = "claude-haiku-4-5-20251001"      # ~$0.25/MTok in
    STANDARD = "claude-sonnet-4-6"           # ~$3/MTok in
    POWERFUL = "claude-opus-4-6"             # ~$15/MTok in


@dataclass
class RoutingDecision:
    tier: Tier
    reason: str
    confidence: float


# Patterns that indicate complexity level
FAST_PATTERNS = [
    re.compile(r'\b(what is|define|meaning of|translate|classify|categorize|yes or no|true or false)\b', re.I),
    re.compile(r'\b(summarize in one|one sentence|single word|one word)\b', re.I),
    re.compile(r'\b(extract|find all|list the|count the)\b', re.I),
]

POWERFUL_PATTERNS = [
    re.compile(r'\b(architect|design a system|comprehensive analysis|in-depth|exhaustive)\b', re.I),
    re.compile(r'\b(compare and contrast|tradeoffs|pros and cons|evaluate)\b', re.I),
    re.compile(r'\b(write a (complete|full|detailed|long))\b', re.I),
    re.compile(r'\b(novel|creative|story|essay|research)\b', re.I),
]


def classify_complexity(message: str) -> RoutingDecision:
    word_count = len(message.split())

    # Short messages are likely simple
    if word_count <= 10:
        for pattern in FAST_PATTERNS:
            if pattern.search(message):
                return RoutingDecision(Tier.FAST, "short + simple pattern", 0.9)
        return RoutingDecision(Tier.FAST, "short message", 0.75)

    # Check for complexity signals
    powerful_matches = sum(1 for p in POWERFUL_PATTERNS if p.search(message))
    fast_matches = sum(1 for p in FAST_PATTERNS if p.search(message))

    if powerful_matches >= 2:
        return RoutingDecision(Tier.POWERFUL, f"{powerful_matches} complexity signals", 0.85)
    if powerful_matches == 1 and word_count > 50:
        return RoutingDecision(Tier.POWERFUL, "complexity signal + long", 0.7)
    if fast_matches >= 1 and word_count < 30:
        return RoutingDecision(Tier.FAST, "simple pattern + short", 0.8)

    # Default: standard for everything else
    return RoutingDecision(Tier.STANDARD, "no strong signals", 0.6)


class TieredRouter:
    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self._stats: dict[str, int] = {t.value: 0 for t in Tier}
        self._costs: dict[str, float] = {
            Tier.FAST.value: 0.25,
            Tier.STANDARD.value: 3.0,
            Tier.POWERFUL.value: 15.0,
        }

    async def complete(
        self,
        user_message: str,
        system: str = "Answer helpfully.",
        max_tokens: int = 512,
        force_tier: Tier | None = None,
    ) -> dict:
        decision = force_tier and RoutingDecision(force_tier, "forced", 1.0) or \
                   classify_complexity(user_message)

        self._stats[decision.tier.value] += 1

        response = await self.client.messages.create(
            model=decision.tier.value,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )

        input_tokens = response.usage.input_tokens
        cost_per_mtok = self._costs[decision.tier.value]
        estimated_cost = (input_tokens / 1_000_000) * cost_per_mtok

        return {
            "text": response.content[0].text,
            "model": decision.tier.value,
            "tier_reason": decision.reason,
            "input_tokens": input_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
        }

    def cost_report(self) -> dict:
        return {
            "model_usage": self._stats,
            "total_requests": sum(self._stats.values()),
        }


# Usage
async def main():
    client = AsyncAnthropic()
    router = TieredRouter(client)

    requests = [
        "What is REST?",
        "Summarize in one sentence: REST APIs use HTTP methods.",
        "Write a comprehensive analysis of microservices vs monolith architectures including cost, scalability, team structure, and operational complexity tradeoffs.",
        "Extract all API endpoints from this text: GET /users, POST /orders, DELETE /items.",
        "Design a distributed rate limiting system for 10M requests/second with sub-millisecond latency.",
    ]

    total_cost = 0.0
    for req in requests:
        result = await router.complete(req)
        total_cost += result["estimated_cost_usd"]
        print(f"[{result['model'].split('-')[1][:5]}|{result['estimated_cost_usd']:.5f}$] "
              f"{req[:50]}: {result['text'][:50]}")

    print(f"\nTotal estimated cost: ${total_cost:.4f}")
    print(f"Usage: {router.cost_report()}")

asyncio.run(main())
```

## Solution 2: LLM-Based Complexity Classifier

Use a lightweight model to classify complexity before routing — more accurate than rules but adds one cheap classification call.

```python
import asyncio
import json
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class ClassificationResult:
    tier: str  # "fast" | "standard" | "powerful"
    reasoning: str
    confidence: float


CLASSIFIER_SYSTEM = """You classify AI request complexity for model routing.

Tiers:
- fast: factual lookup, one-word answers, simple extraction, yes/no, definitions
- standard: code generation, multi-step explanations, summaries, comparisons
- powerful: system design, creative writing, novel reasoning, comprehensive analysis

Reply with JSON only:
{"tier": "fast|standard|powerful", "reasoning": "one sentence", "confidence": 0.0-1.0}"""


async def llm_classify(client: AsyncAnthropic, user_message: str) -> ClassificationResult:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # Always use cheapest for classification
        max_tokens=80,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": user_message[:500]}],  # Truncate for speed
    )

    try:
        text = response.content[0].text.strip()
        # Extract JSON even if wrapped in markdown
        json_match = re.search(r'\{.*?\}', text, re.DOTALL)
        data = json.loads(json_match.group() if json_match else text)
        return ClassificationResult(
            tier=data.get("tier", "standard"),
            reasoning=data.get("reasoning", ""),
            confidence=float(data.get("confidence", 0.7)),
        )
    except Exception:
        return ClassificationResult("standard", "parse_error", 0.5)


MODEL_MAP = {
    "fast": "claude-haiku-4-5-20251001",
    "standard": "claude-sonnet-4-6",
    "powerful": "claude-opus-4-6",
}


class LLMClassifiedRouter:
    def __init__(self, client: AsyncAnthropic, min_confidence: float = 0.6):
        self.client = client
        self.min_confidence = min_confidence
        self._routing_log: list[dict] = []

    async def complete(
        self,
        user_message: str,
        system: str = "Answer helpfully.",
        max_tokens: int = 512,
    ) -> dict:
        # Classify (in parallel with nothing — fast)
        classification = await llm_classify(self.client, user_message)

        # Fall back to standard if not confident
        if classification.confidence < self.min_confidence:
            model = MODEL_MAP["standard"]
            classification.tier = "standard"
        else:
            model = MODEL_MAP.get(classification.tier, MODEL_MAP["standard"])

        self._routing_log.append({
            "tier": classification.tier,
            "model": model,
            "confidence": classification.confidence,
            "reasoning": classification.reasoning,
        })

        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )

        return {
            "text": response.content[0].text,
            "model": model,
            "tier": classification.tier,
            "classification_reason": classification.reasoning,
            "confidence": classification.confidence,
        }

    def routing_summary(self) -> dict:
        tier_counts: dict[str, int] = {}
        for entry in self._routing_log:
            tier_counts[entry["tier"]] = tier_counts.get(entry["tier"], 0) + 1
        return {"total": len(self._routing_log), "by_tier": tier_counts}


# Usage
async def main():
    client = AsyncAnthropic()
    router = LLMClassifiedRouter(client)

    requests = [
        "What year was Python created?",
        "Write a Python function to implement binary search with proper error handling.",
        "Design an event-driven microservices architecture for a high-traffic e-commerce platform.",
    ]

    for req in requests:
        result = await router.complete(req)
        print(f"[{result['tier']}|conf={result['confidence']:.2f}] {req[:50]}")
        print(f"  Reason: {result['classification_reason']}")
        print(f"  Answer: {result['text'][:80]}\n")

    print(f"Routing summary: {router.routing_summary()}")

asyncio.run(main())
```

## Solution 3: Cost-Aware Tiering with Per-User Budget

Track cumulative cost per user and automatically downgrade to cheaper models when a user approaches their budget — maintaining service while controlling costs.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field


@dataclass
class UserBudget:
    user_id: str
    monthly_budget_usd: float
    spent_usd: float = 0.0
    request_count: int = 0
    window_start: float = field(default_factory=time.time)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.monthly_budget_usd - self.spent_usd)

    @property
    def utilization(self) -> float:
        return self.spent_usd / max(self.monthly_budget_usd, 0.01)

    def charge(self, amount_usd: float):
        self.spent_usd += amount_usd
        self.request_count += 1


# Approximate cost per 1k tokens (in + out combined estimate)
TIER_COSTS = {
    "claude-haiku-4-5-20251001": 0.001,   # ~$1/MTok blended
    "claude-sonnet-4-6": 0.015,           # ~$15/MTok blended
    "claude-opus-4-6": 0.075,             # ~$75/MTok blended
}


def budget_adjusted_model(
    requested_model: str,
    budget: UserBudget,
) -> str:
    """Downgrade model if user budget is constrained."""
    utilization = budget.utilization

    if utilization >= 0.95:
        return "claude-haiku-4-5-20251001"  # Force cheapest when near limit
    elif utilization >= 0.75 and requested_model == "claude-opus-4-6":
        print(f"[budget] {budget.user_id}: downgrading Opus→Sonnet (util={utilization:.0%})")
        return "claude-sonnet-4-6"
    elif utilization >= 0.90 and requested_model in ("claude-opus-4-6", "claude-sonnet-4-6"):
        print(f"[budget] {budget.user_id}: downgrading to Haiku (util={utilization:.0%})")
        return "claude-haiku-4-5-20251001"

    return requested_model


class BudgetAwareTieredAgent:
    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self._budgets: dict[str, UserBudget] = {}

    def get_or_create_budget(self, user_id: str, monthly_limit: float = 10.0) -> UserBudget:
        if user_id not in self._budgets:
            self._budgets[user_id] = UserBudget(user_id, monthly_limit)
        return self._budgets[user_id]

    async def complete(
        self,
        user_id: str,
        user_message: str,
        preferred_model: str = "claude-sonnet-4-6",
        system: str = "Answer helpfully.",
        max_tokens: int = 256,
    ) -> dict:
        budget = self.get_or_create_budget(user_id)

        if budget.remaining_usd <= 0:
            return {
                "status": "budget_exhausted",
                "spent": round(budget.spent_usd, 4),
                "limit": budget.monthly_budget_usd,
            }

        model = budget_adjusted_model(preferred_model, budget)

        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )

        tokens_used = response.usage.input_tokens + response.usage.output_tokens
        cost = (tokens_used / 1000) * TIER_COSTS.get(model, 0.015)
        budget.charge(cost)

        return {
            "status": "ok",
            "text": response.content[0].text,
            "model_used": model,
            "preferred_model": preferred_model,
            "downgraded": model != preferred_model,
            "cost_usd": round(cost, 6),
            "budget_remaining": round(budget.remaining_usd, 4),
            "budget_util": round(budget.utilization, 3),
        }


# Usage
async def main():
    client = AsyncAnthropic()
    agent = BudgetAwareTieredAgent(client)

    # Simulate user with tight budget
    user = "user_tight"
    agent.get_or_create_budget(user, monthly_limit=0.05)  # Tiny budget

    for i in range(5):
        result = await agent.complete(
            user,
            f"Question {i}: explain REST APIs in detail with examples.",
            preferred_model="claude-opus-4-6",
        )
        print(f"[req {i+1}] model={result.get('model_used', '').split('-')[1][:5]} "
              f"downgraded={result.get('downgraded')} "
              f"cost=${result.get('cost_usd', 0):.5f} "
              f"remaining=${result.get('budget_remaining', 0):.4f}")

asyncio.run(main())
```

## Solution 4: Quality-Validated Tiering — Promote if Fast Tier Fails Quality Check

Route to Fast tier first; if the response fails a quality gate, automatically promote and retry with a higher tier — optimistic cost minimization with quality safety net.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class TieredResult:
    text: str
    model_used: str
    promoted: bool
    quality_score: float
    attempts: int


def score_response(text: str, min_length: int = 30, required_keywords: list[str] | None = None) -> float:
    """Simple quality heuristic. Replace with LLM-as-judge in production."""
    score = 1.0

    if len(text) < min_length:
        score -= 0.4
    if not text.strip():
        score = 0.0
    if re.match(r"^(I (don't|cannot|can't)|Sorry|Unfortunately)", text, re.I):
        score -= 0.3

    for kw in (required_keywords or []):
        if kw.lower() not in text.lower():
            score -= 0.15

    return max(0.0, min(1.0, score))


async def tiered_with_promotion(
    client: AsyncAnthropic,
    user_message: str,
    system: str = "Answer helpfully.",
    max_tokens: int = 256,
    quality_threshold: float = 0.6,
    required_keywords: list[str] | None = None,
) -> TieredResult:
    tier_sequence = [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ]

    for attempt, model in enumerate(tier_sequence):
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        quality = score_response(text, required_keywords=required_keywords)

        if quality >= quality_threshold or model == tier_sequence[-1]:
            return TieredResult(
                text=text,
                model_used=model,
                promoted=attempt > 0,
                quality_score=quality,
                attempts=attempt + 1,
            )

        print(f"[tiering] {model} quality={quality:.2f} < {quality_threshold} — promoting to next tier")

    # Unreachable but satisfies type checker
    raise RuntimeError("Tiering exhausted")


# Usage
async def main():
    client = AsyncAnthropic()

    test_cases = [
        ("What is 2+2?", []),
        ("Explain idempotency in REST APIs.", ["idempotent", "HTTP", "PUT"]),
        ("Write a comprehensive comparison of CAP theorem implications for Cassandra vs DynamoDB.", ["partition", "consistency", "availability"]),
    ]

    for question, required_kws in test_cases:
        result = await tiered_with_promotion(
            client, question, required_keywords=required_kws, quality_threshold=0.6
        )
        tier_label = result.model_used.split('-')[1][:5]
        promoted = "↑" if result.promoted else "─"
        print(f"[{promoted}{tier_label}|q={result.quality_score:.2f}|n={result.attempts}] "
              f"{question[:50]}: {result.text[:60]}")

asyncio.run(main())
```

## Solution 5: Dynamic Tiering Based on Response Feedback Loop

Adjust tier selection over time based on observed quality and user feedback — automatically finding the lowest tier that maintains acceptable quality for each query type.

```python
import asyncio
import statistics
from anthropic import AsyncAnthropic
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class TierPerformance:
    model: str
    quality_scores: list[float] = field(default_factory=list)
    total_cost: float = 0.0
    requests: int = 0

    @property
    def avg_quality(self) -> float:
        return statistics.mean(self.quality_scores) if self.quality_scores else 0.0

    @property
    def cost_per_quality_point(self) -> float:
        return self.total_cost / max(self.avg_quality * self.requests, 0.001)


TIER_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]

TIER_COST_PER_1K = {
    "claude-haiku-4-5-20251001": 0.001,
    "claude-sonnet-4-6": 0.015,
    "claude-opus-4-6": 0.075,
}


class AdaptiveTieringAgent:
    def __init__(
        self,
        client: AsyncAnthropic,
        quality_threshold: float = 0.7,
        exploration_rate: float = 0.1,
    ):
        self.client = client
        self.quality_threshold = quality_threshold
        self.exploration_rate = exploration_rate
        self._performance: dict[str, dict[str, TierPerformance]] = defaultdict(
            lambda: {m: TierPerformance(m) for m in TIER_MODELS}
        )

    def _query_type(self, message: str) -> str:
        """Simple query type bucketing."""
        words = len(message.split())
        if words <= 15:
            return "short"
        elif words <= 50:
            return "medium"
        return "long"

    def _best_model_for_type(self, query_type: str) -> str:
        """Select cheapest model meeting quality threshold."""
        import random
        if random.random() < self.exploration_rate:
            return random.choice(TIER_MODELS)

        perfs = self._performance[query_type]
        # Among models with enough data and sufficient quality
        qualified = [
            m for m in TIER_MODELS
            if perfs[m].requests >= 3 and perfs[m].avg_quality >= self.quality_threshold
        ]

        if not qualified:
            return "claude-sonnet-4-6"  # Default until we have data

        # Pick cheapest qualified model
        return min(qualified, key=lambda m: TIER_COST_PER_1K[m])

    def record_feedback(self, query_type: str, model: str, quality: float, tokens: int):
        perf = self._performance[query_type][model]
        perf.quality_scores.append(quality)
        perf.total_cost += (tokens / 1000) * TIER_COST_PER_1K[model]
        perf.requests += 1

    async def complete(self, user_message: str, system: str = "Answer helpfully.", max_tokens: int = 256) -> dict:
        query_type = self._query_type(user_message)
        model = self._best_model_for_type(query_type)

        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        tokens = response.usage.input_tokens + response.usage.output_tokens

        # Simple quality proxy
        quality = min(len(text) / 100.0, 1.0)
        self.record_feedback(query_type, model, quality, tokens)

        return {
            "text": text,
            "model": model,
            "query_type": query_type,
            "quality_estimate": round(quality, 2),
        }

    def performance_report(self) -> dict:
        report = {}
        for qtype, perfs in self._performance.items():
            report[qtype] = {
                m: {"avg_quality": round(p.avg_quality, 2), "requests": p.requests}
                for m, p in perfs.items() if p.requests > 0
            }
        return report


# Usage
async def main():
    client = AsyncAnthropic()
    agent = AdaptiveTieringAgent(client, quality_threshold=0.5, exploration_rate=0.2)

    requests = [
        "What is REST?",
        "Explain caching.",
        "What is OAuth 2.0?",
        "Describe the CAP theorem and its practical implications for distributed database design.",
        "What is GraphQL?",
        "Explain the tradeoffs between microservices and monolith architectures in detail.",
    ]

    for req in requests:
        result = await agent.complete(req)
        print(f"[{result['query_type']}|{result['model'].split('-')[1][:5]}] {req[:50]}")

    print(f"\nPerformance report: {agent.performance_report()}")

asyncio.run(main())
```

## Solution 6: Prompt-Size-Aware Tiering

Select model tier based on combined prompt token count — small prompts to cheap models, large context prompts to context-capable models with appropriate pricing.

```python
import asyncio
from anthropic import AsyncAnthropic, Anthropic
from dataclasses import dataclass


@dataclass
class ContextAwareRouting:
    model: str
    reason: str
    estimated_input_tokens: int


def route_by_context_size(
    input_tokens: int,
    complexity_hint: str = "auto",
) -> ContextAwareRouting:
    """
    Route based on context size and complexity:
    - Small context + simple → Haiku
    - Large context → Sonnet (better context utilization)
    - Very long + complex → Sonnet/Opus
    """
    if complexity_hint == "simple" or input_tokens < 500:
        return ContextAwareRouting(
            "claude-haiku-4-5-20251001",
            f"small context ({input_tokens} tokens)",
            input_tokens,
        )
    elif input_tokens < 8000:
        return ContextAwareRouting(
            "claude-sonnet-4-6",
            f"medium context ({input_tokens} tokens)",
            input_tokens,
        )
    else:
        return ContextAwareRouting(
            "claude-sonnet-4-6",
            f"large context ({input_tokens} tokens)",
            input_tokens,
        )


class ContextAwareTieredAgent:
    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self._sync_client = Anthropic()

    def _estimate_tokens(self, messages: list[dict], system: str) -> int:
        response = self._sync_client.messages.count_tokens(
            model="claude-haiku-4-5-20251001",
            system=system,
            messages=messages,
        )
        return response.input_tokens

    async def complete(
        self,
        user_message: str,
        system: str = "Answer helpfully.",
        context_documents: list[str] | None = None,
        max_tokens: int = 512,
        complexity_hint: str = "auto",
    ) -> dict:
        # Build full prompt with any context
        full_message = user_message
        if context_documents:
            docs_str = "\n\n".join(
                f"<doc_{i}>\n{doc}\n</doc_{i}>"
                for i, doc in enumerate(context_documents)
            )
            full_message = f"{docs_str}\n\n{user_message}"

        messages = [{"role": "user", "content": full_message}]
        input_tokens = self._estimate_tokens(messages, system)

        routing = route_by_context_size(input_tokens, complexity_hint)

        response = await self.client.messages.create(
            model=routing.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )

        return {
            "text": response.content[0].text,
            "model": routing.model,
            "routing_reason": routing.reason,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }


# Usage
async def main():
    client = AsyncAnthropic()
    agent = ContextAwareTieredAgent(client)

    # Short prompt
    r1 = await agent.complete("What is REST?")
    print(f"Short: [{r1['model'].split('-')[1][:5]}] {r1['input_tokens']} tokens | {r1['text'][:60]}")

    # Long context
    big_doc = "This is a long document about REST APIs. " * 100
    r2 = await agent.complete(
        "Summarize the key points.",
        context_documents=[big_doc],
    )
    print(f"Large ctx: [{r2['model'].split('-')[1][:5]}] {r2['input_tokens']} tokens | {r2['text'][:60]}")

asyncio.run(main())
```

## Comparison

| Approach | Classification Cost | Accuracy | Adaptability | Complexity | Best For |
|---|---|---|---|---|---|
| Rule-Based Router | Zero | Medium | None | Very Low | Most workloads, quick win |
| LLM Classifier | 1 cheap model call | High | None | Low | Diverse request types |
| Budget-Aware Tiering | Zero | Medium | Yes | Low | Multi-tenant cost control |
| Quality-Validated Promotion | 1–3 calls | Very High | None | Medium | Quality-critical pipelines |
| Feedback Loop Adaptation | Zero overhead | High | Yes | High | Long-running agents |
| Context-Size Routing | Token count call | High | None | Low | RAG / document agents |
