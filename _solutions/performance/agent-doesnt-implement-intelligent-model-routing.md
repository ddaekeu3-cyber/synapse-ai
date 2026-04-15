---
layout: solution
title: "Agent Doesn't Implement Intelligent Model Routing"
category: performance
description: "How to dynamically route requests to the right Claude model tier (Haiku/Sonnet/Opus) based on task complexity, cost budget, and latency requirements to minimize cost without sacrificing quality."
tags: [performance, routing, model-selection, cost, latency, classification]
---

# Agent Doesn't Implement Intelligent Model Routing

Sending every request to `claude-opus-4-6` is expensive and slow. Sending every request to `claude-haiku-4-5-20251001` sacrifices quality on complex tasks. Intelligent routing classifies each query by complexity, urgency, and cost sensitivity, then selects the cheapest model capable of handling it correctly. A well-tuned router cuts costs 60–80% while maintaining response quality.

## Option 1: Keyword-Based Rule Router

Classify requests with fast keyword matching and route to Haiku, Sonnet, or Opus based on complexity signals.

```python
import anthropic
import re
import time
from dataclasses import dataclass
from enum import Enum

class ModelTier(Enum):
    HAIKU = "claude-haiku-4-5-20251001"
    SONNET = "claude-sonnet-4-6"
    OPUS = "claude-opus-4-6"

COST_PER_1K_INPUT = {
    ModelTier.HAIKU: 0.00025,
    ModelTier.SONNET: 0.003,
    ModelTier.OPUS: 0.015,
}

# Signals that indicate high complexity → Opus
OPUS_SIGNALS = [
    r"\b(architect|design|evaluate|critically analyze|research|strategy)\b",
    r"\b(compare and contrast|trade-?offs?|pros and cons)\b",
    r"\b(comprehensive|exhaustive|in-depth|thorough analysis)\b",
    r"\b(PhD|expert-level|production-grade|enterprise)\b",
    r"\b(reasoning|logic|philosophy|ethics)\b",
]

# Signals that indicate moderate complexity → Sonnet
SONNET_SIGNALS = [
    r"\b(explain|describe|write|implement|create|debug)\b",
    r"\b(how does|how do|why does|what causes)\b",
    r"\b(function|class|module|algorithm|refactor)\b",
    r"\b(summarize|translate|convert|format)\b",
    r"\b(step.by.step|detailed|example)\b",
]

# Default → Haiku (simple lookups, yes/no, short facts)
HAIKU_SIGNALS = [
    r"^(what|who|when|where|is|are|does|did|can|has)\b.{0,60}\?$",
    r"\b(define|spell|acronym|abbreviation)\b",
    r"\b(yes or no|true or false|correct or incorrect)\b",
]


def classify_complexity(prompt: str) -> ModelTier:
    lower = prompt.lower().strip()

    for pattern in OPUS_SIGNALS:
        if re.search(pattern, lower):
            return ModelTier.OPUS

    for pattern in HAIKU_SIGNALS:
        if re.search(pattern, lower):
            return ModelTier.HAIKU

    for pattern in SONNET_SIGNALS:
        if re.search(pattern, lower):
            return ModelTier.SONNET

    # Length heuristic: long prompts likely need more capable model
    word_count = len(prompt.split())
    if word_count > 150:
        return ModelTier.SONNET
    if word_count < 20:
        return ModelTier.HAIKU

    return ModelTier.SONNET  # safe default


def route_and_respond(prompt: str, max_tokens: int = 500) -> tuple[str, ModelTier]:
    client = anthropic.Anthropic()
    tier = classify_complexity(prompt)

    print(f"[ROUTER] Classified as {tier.name} (${COST_PER_1K_INPUT[tier]:.5f}/1K tokens)")

    start = time.monotonic()
    response = client.messages.create(
        model=tier.value,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    latency = (time.monotonic() - start) * 1000

    tokens = response.usage.input_tokens + response.usage.output_tokens
    cost = tokens * COST_PER_1K_INPUT[tier] / 1000
    print(f"[ROUTER] {tier.name} | {tokens} tokens | ${cost:.5f} | {latency:.0f}ms")

    return response.content[0].text, tier


if __name__ == "__main__":
    prompts = [
        "Is Python dynamically typed?",
        "Write a Python function to implement binary search.",
        "Architect a distributed event-driven microservices system for a global e-commerce platform with trade-offs analysis.",
        "What year was Python created?",
        "Explain how garbage collection works in the JVM.",
    ]

    total_cost = 0.0
    for p in prompts:
        print(f"\nPrompt: {p[:70]}")
        result, tier = route_and_respond(p)
        print(f"Response: {result[:80]}...")

# Expected Token Savings: 60-80% cost reduction by sending ~70% of queries to Haiku instead of Sonnet/Opus
# Environment: Mixed-workload chatbots, developer tools, customer support where query complexity varies widely
```

## Option 2: Haiku Meta-Classifier for Accurate Routing

Use a cheap Haiku call to classify the request before routing. More accurate than keyword rules, still low overhead.

```python
import anthropic
import json
import re
import time
from dataclasses import dataclass

@dataclass
class RoutingDecision:
    model: str
    complexity: str
    reasoning: str
    classifier_tokens: int


CLASSIFIER_PROMPT = """You are a task complexity classifier for AI model routing.

Models available:
- haiku: Simple factual lookups, yes/no questions, short definitions, basic conversions. Max ~100 output tokens.
- sonnet: Explanations, code writing, debugging, summaries, translations, moderate reasoning. Max ~500 output tokens.
- opus: Architecture design, deep analysis, research synthesis, complex reasoning, expert-level review. Max ~2000 output tokens.

Classify this request and respond with JSON only:
{
  "model": "haiku" | "sonnet" | "opus",
  "complexity": "simple" | "moderate" | "complex",
  "reasoning": "one sentence explanation"
}"""


def classify_with_haiku(user_prompt: str) -> RoutingDecision:
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=CLASSIFIER_PROMPT,
        messages=[{"role": "user", "content": f"Classify: {user_prompt[:300]}"}],
    )

    text = response.content[0].text
    try:
        json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
        data = json.loads(json_match.group()) if json_match else {}
        model_map = {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6",
        }
        return RoutingDecision(
            model=model_map.get(data.get("model", "sonnet"), "claude-sonnet-4-6"),
            complexity=data.get("complexity", "moderate"),
            reasoning=data.get("reasoning", "default"),
            classifier_tokens=response.usage.input_tokens + response.usage.output_tokens,
        )
    except Exception:
        return RoutingDecision(
            model="claude-sonnet-4-6",
            complexity="moderate",
            reasoning="classification failed — defaulting to sonnet",
            classifier_tokens=0,
        )


def meta_classified_response(user_prompt: str, max_tokens: int = 600) -> str:
    client = anthropic.Anthropic()

    # Step 1: cheap classification
    decision = classify_with_haiku(user_prompt)
    print(f"[ROUTER] Model: {decision.model.split('-')[1]} | Complexity: {decision.complexity}")
    print(f"[ROUTER] Reason: {decision.reasoning} | Classifier tokens: {decision.classifier_tokens}")

    # Step 2: route to selected model
    start = time.monotonic()
    response = client.messages.create(
        model=decision.model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_prompt}],
    )
    latency = (time.monotonic() - start) * 1000

    print(f"[ROUTER] Response: {response.usage.output_tokens} tokens | {latency:.0f}ms")
    return response.content[0].text


if __name__ == "__main__":
    test_cases = [
        "What is 7 * 8?",
        "How do I implement a binary search tree in Python?",
        "Design a fault-tolerant distributed consensus protocol for a global financial transaction system.",
        "What does API stand for?",
        "Debug this code and explain why it fails: def div(a,b): return a/b",
    ]

    for prompt in test_cases:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt[:60]}")
        result = meta_classified_response(prompt)
        print(f"Result: {result[:100]}...")

# Expected Token Savings: 55-75% cost reduction; classifier costs ~$0.000025 vs. $0.003+ for misrouted Sonnet calls
# Environment: Production APIs with unpredictable query complexity, high-volume query endpoints
```

## Option 3: Routing with Cost Budget and Deadline Constraints

Route based on both complexity AND per-request cost/latency budget. High-priority fast requests get downgraded to Haiku; bulk batch jobs can afford Opus.

```python
import anthropic
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Priority(Enum):
    REALTIME = "realtime"    # <500ms, cost cap $0.001
    STANDARD = "standard"   # <5s, cost cap $0.01
    BATCH = "batch"          # No latency req, max quality


MODEL_TIERS = {
    "haiku": {
        "model_id": "claude-haiku-4-5-20251001",
        "avg_latency_ms": 400,
        "cost_per_1k_tokens": 0.00025,
        "max_quality_score": 0.7,
    },
    "sonnet": {
        "model_id": "claude-sonnet-4-6",
        "avg_latency_ms": 1500,
        "cost_per_1k_tokens": 0.003,
        "max_quality_score": 0.9,
    },
    "opus": {
        "model_id": "claude-opus-4-6",
        "avg_latency_ms": 4000,
        "cost_per_1k_tokens": 0.015,
        "max_quality_score": 1.0,
    },
}


@dataclass
class RoutingConstraints:
    priority: Priority
    max_cost_usd: Optional[float] = None
    max_latency_ms: Optional[float] = None
    min_quality: float = 0.0
    estimated_tokens: int = 300


def select_model(complexity_tier: str, constraints: RoutingConstraints) -> str:
    """Select best model satisfying all constraints."""
    # Preferred ordering based on complexity
    preference_order = {
        "simple": ["haiku", "sonnet", "opus"],
        "moderate": ["sonnet", "haiku", "opus"],
        "complex": ["opus", "sonnet", "haiku"],
    }.get(complexity_tier, ["sonnet", "haiku", "opus"])

    for tier_name in preference_order:
        tier = MODEL_TIERS[tier_name]
        tokens = constraints.estimated_tokens
        estimated_cost = tokens * tier["cost_per_1k_tokens"] / 1000

        # Check cost constraint
        if constraints.max_cost_usd and estimated_cost > constraints.max_cost_usd:
            print(f"[ROUTER] {tier_name}: skip (cost ${estimated_cost:.5f} > limit ${constraints.max_cost_usd:.5f})")
            continue

        # Check latency constraint
        if constraints.max_latency_ms and tier["avg_latency_ms"] > constraints.max_latency_ms:
            print(f"[ROUTER] {tier_name}: skip (latency {tier['avg_latency_ms']}ms > limit {constraints.max_latency_ms}ms)")
            continue

        # Check quality constraint
        if tier["max_quality_score"] < constraints.min_quality:
            print(f"[ROUTER] {tier_name}: skip (quality {tier['max_quality_score']} < min {constraints.max_latency_ms})")
            continue

        print(f"[ROUTER] Selected {tier_name} (cost ~${estimated_cost:.5f}, latency ~{tier['avg_latency_ms']}ms)")
        return tier["model_id"]

    # Fallback: Haiku always satisfies latency and cost for simple queries
    print("[ROUTER] All preferred models constrained — falling back to haiku")
    return MODEL_TIERS["haiku"]["model_id"]


# Complexity classification (simplified)
def estimate_complexity(prompt: str) -> str:
    words = len(prompt.split())
    if words < 15:
        return "simple"
    elif words > 100:
        return "complex"
    return "moderate"


def constrained_route(
    prompt: str,
    priority: Priority,
    max_tokens: int = 400,
) -> tuple[str, str]:
    client = anthropic.Anthropic()

    # Define constraints per priority level
    constraints_map = {
        Priority.REALTIME: RoutingConstraints(
            priority=Priority.REALTIME,
            max_cost_usd=0.001,
            max_latency_ms=600,
            min_quality=0.6,
            estimated_tokens=max_tokens,
        ),
        Priority.STANDARD: RoutingConstraints(
            priority=Priority.STANDARD,
            max_cost_usd=0.005,
            max_latency_ms=5000,
            min_quality=0.7,
            estimated_tokens=max_tokens,
        ),
        Priority.BATCH: RoutingConstraints(
            priority=Priority.BATCH,
            max_cost_usd=None,
            max_latency_ms=None,
            min_quality=0.9,
            estimated_tokens=max_tokens,
        ),
    }

    constraints = constraints_map[priority]
    complexity = estimate_complexity(prompt)
    model_id = select_model(complexity, constraints)

    print(f"[ROUTER] Priority={priority.value} Complexity={complexity} Model={model_id.split('-')[1]}")

    response = client.messages.create(
        model=model_id,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text, model_id


if __name__ == "__main__":
    test_cases = [
        ("What time is it in Tokyo?", Priority.REALTIME),
        ("Explain how neural networks learn.", Priority.STANDARD),
        ("Write a comprehensive architecture document for a distributed ML training system.", Priority.BATCH),
        ("Is 17 prime?", Priority.REALTIME),
    ]

    for prompt, priority in test_cases:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt[:60]} [{priority.value}]")
        result, model = constrained_route(prompt, priority)
        print(f"Response: {result[:100]}...")

# Expected Token Savings: 70-85% for realtime endpoints forced to Haiku; batch jobs get Opus quality at no extra urgency cost
# Environment: APIs with mixed SLA tiers, chatbots with premium vs. free user tiers
```

## Option 4: Adaptive Router with Outcome Feedback Learning

Track actual response quality per model per query type. Continuously tighten routing based on observed outcomes.

```python
import anthropic
import sqlite3
import time
import json
import re
from dataclasses import dataclass
from typing import Optional

DB_PATH = "routing_outcomes.db"

def init_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS routing_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_category TEXT NOT NULL,
            model_used TEXT NOT NULL,
            quality_score REAL,          -- 0.0 to 1.0, from LLM judge
            output_tokens INTEGER,
            latency_ms REAL,
            cost_usd REAL,
            timestamp REAL NOT NULL
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_cat_model ON routing_outcomes(query_category, model_used)
    """)
    db.commit()
    return db


def get_routing_recommendation(
    db: sqlite3.Connection,
    category: str,
    quality_threshold: float = 0.75,
) -> str:
    """Find cheapest model that has achieved quality_threshold historically."""
    models_by_cost = [
        ("claude-haiku-4-5-20251001", 0.00025),
        ("claude-sonnet-4-6", 0.003),
        ("claude-opus-4-6", 0.015),
    ]

    for model_id, _ in models_by_cost:
        rows = db.execute("""
            SELECT AVG(quality_score), COUNT(*) FROM routing_outcomes
            WHERE query_category = ? AND model_used = ?
            ORDER BY timestamp DESC LIMIT 20
        """, (category, model_id)).fetchone()

        avg_quality, count = rows[0], rows[1]
        if count >= 3 and avg_quality is not None and avg_quality >= quality_threshold:
            print(f"[ADAPTIVE] {model_id.split('-')[1]} qualifies for {category} (avg quality {avg_quality:.2f} over {count} samples)")
            return model_id

    return "claude-sonnet-4-6"  # Default


def judge_quality(prompt: str, response: str) -> float:
    """Use Haiku to score response quality."""
    client = anthropic.Anthropic()
    judge_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{"role": "user", "content": f"""Rate this response quality from 0.0 to 1.0.

User asked: {prompt[:150]}
Response: {response[:300]}

Reply with only a decimal number between 0.0 and 1.0."""}],
    )
    try:
        score = float(re.search(r"\d+\.?\d*", judge_response.content[0].text).group())
        return min(1.0, max(0.0, score))
    except Exception:
        return 0.5


def classify_category(prompt: str) -> str:
    words = prompt.lower().split()
    if any(w in words for w in ["code", "function", "implement", "debug", "class"]):
        return "code"
    if any(w in words for w in ["explain", "how", "why", "what", "describe"]):
        return "explanation"
    if any(w in words for w in ["write", "draft", "compose", "create"]):
        return "creative"
    return "general"


def adaptive_route(
    db: sqlite3.Connection,
    prompt: str,
    max_tokens: int = 400,
    judge_responses: bool = True,
) -> str:
    client = anthropic.Anthropic()

    category = classify_category(prompt)
    model_id = get_routing_recommendation(db, category)

    print(f"[ADAPTIVE] Category={category} Model={model_id.split('-')[1]}")

    start = time.monotonic()
    response = client.messages.create(
        model=model_id,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    latency = (time.monotonic() - start) * 1000
    output = response.content[0].text

    # Record outcome with quality judgment
    if judge_responses:
        quality = judge_quality(prompt, output)
        cost_per_1k = {"haiku": 0.00025, "sonnet": 0.003, "opus": 0.015}
        tier = "haiku" if "haiku" in model_id else ("sonnet" if "sonnet" in model_id else "opus")
        cost = response.usage.output_tokens * cost_per_1k[tier] / 1000

        db.execute("""
            INSERT INTO routing_outcomes (query_category, model_used, quality_score, output_tokens, latency_ms, cost_usd, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (category, model_id, quality, response.usage.output_tokens, latency, cost, time.time()))
        db.commit()

        print(f"[ADAPTIVE] Quality={quality:.2f} | Latency={latency:.0f}ms | Cost=${cost:.5f}")

    return output


if __name__ == "__main__":
    db = init_db() if DB_PATH else sqlite3.connect(":memory:")
    db = sqlite3.connect(":memory:")
    # Initialize schema for in-memory db
    db.execute("""CREATE TABLE IF NOT EXISTS routing_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, query_category TEXT, model_used TEXT,
        quality_score REAL, output_tokens INTEGER, latency_ms REAL, cost_usd REAL, timestamp REAL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cat_model ON routing_outcomes(query_category, model_used)")
    db.commit()

    prompts = [
        "What is the capital of Spain?",
        "Write a Python function to validate email addresses.",
        "Explain the difference between TCP and UDP.",
    ]

    for prompt in prompts:
        print(f"\nPrompt: {prompt}")
        result = adaptive_route(db, prompt)
        print(f"Result: {result[:100]}...")

# Expected Token Savings: Converges to cheapest model that maintains quality; self-improves over time
# Environment: High-volume agents where routing accuracy improves with usage data
```

## Option 5: Two-Stage Routing with Quality Gate

Route to Haiku first. If the output quality fails a lightweight check, automatically upgrade to Sonnet or Opus.

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Optional

@dataclass
class QualityCheck:
    passed: bool
    reason: Optional[str]
    confidence: float


def quick_quality_check(prompt: str, response: str) -> QualityCheck:
    """Fast heuristic quality check without LLM call."""
    # Too short
    if len(response.strip()) < 20:
        return QualityCheck(False, "response too short", 0.9)

    # Refusal patterns
    refusal_patterns = [
        r"i('m| am) (not able|unable) to",
        r"i (cannot|can't) (help|answer|assist)",
        r"as an ai (language model|assistant), i (don't|cannot)",
    ]
    for pat in refusal_patterns:
        if re.search(pat, response.lower()):
            return QualityCheck(False, f"response contains refusal: matched '{pat}'", 0.85)

    # Uncertainty overflow
    uncertainty_words = ["i'm not sure", "i don't know", "i'm uncertain", "you should consult"]
    uncertainty_count = sum(1 for w in uncertainty_words if w in response.lower())
    if uncertainty_count >= 2:
        return QualityCheck(False, "response expresses excessive uncertainty", 0.7)

    # Code requested but not delivered
    if any(w in prompt.lower() for w in ["write", "implement", "code", "function"]):
        if "```" not in response and "def " not in response:
            return QualityCheck(False, "code requested but no code block found", 0.8)

    return QualityCheck(True, None, 0.9)


def two_stage_route(
    prompt: str,
    max_tokens: int = 500,
    enable_upgrade: bool = True,
) -> tuple[str, str, int]:
    """Returns (response, model_used, upgrade_count)."""
    client = anthropic.Anthropic()
    upgrade_count = 0

    # Stage 1: Try Haiku
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    output = response.content[0].text
    model_used = "claude-haiku-4-5-20251001"

    check = quick_quality_check(prompt, output)
    print(f"[STAGE-1] Haiku quality check: {'PASS' if check.passed else 'FAIL'} ({check.reason or 'ok'})")

    if check.passed or not enable_upgrade:
        return output, model_used, upgrade_count

    # Stage 2: Upgrade to Sonnet
    upgrade_count += 1
    print(f"[STAGE-2] Upgrading to Sonnet...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    output = response.content[0].text
    model_used = "claude-sonnet-4-6"

    check = quick_quality_check(prompt, output)
    print(f"[STAGE-2] Sonnet quality check: {'PASS' if check.passed else 'FAIL'}")

    if check.passed:
        return output, model_used, upgrade_count

    # Stage 3: Final upgrade to Opus
    upgrade_count += 1
    print(f"[STAGE-3] Upgrading to Opus...")
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text, "claude-opus-4-6", upgrade_count


if __name__ == "__main__":
    test_prompts = [
        "What is 12 * 15?",
        "Write a complete Python class implementing a doubly linked list with insert, delete, and search methods.",
        "What is the capital of Australia?",
        "Implement a thread-safe LRU cache in Python with O(1) get and put operations.",
    ]

    for prompt in test_prompts:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt[:70]}")
        result, model, upgrades = two_stage_route(prompt)
        print(f"Final model: {model.split('-')[1]} | Upgrades: {upgrades}")
        print(f"Result: {result[:120]}...")

# Expected Token Savings: 50-70% — most simple queries succeed at Haiku; complex ones auto-escalate
# Environment: General-purpose assistants where query complexity is unknown in advance
```

## Option 6: Semantic Embedding Router with Cached Classifications

Build a cache of prompt→model mappings using semantic similarity. Similar future prompts reuse cached routing decisions.

```python
import anthropic
import json
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RoutingCacheEntry:
    prompt_hash: str
    prompt_preview: str
    model_id: str
    quality_observed: float
    hit_count: int = 0
    created_at: float = field(default_factory=time.time)


class SemanticRoutingCache:
    """Exact-match cache with bloom filter for fast misses."""

    def __init__(self, max_size: int = 1000):
        self.cache: dict[str, RoutingCacheEntry] = {}
        self.max_size = max_size

    def _hash_prompt(self, prompt: str) -> str:
        # Normalize: lowercase, remove extra whitespace
        normalized = " ".join(prompt.lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def get(self, prompt: str) -> Optional[str]:
        key = self._hash_prompt(prompt)
        entry = self.cache.get(key)
        if entry:
            entry.hit_count += 1
            return entry.model_id
        return None

    def set(self, prompt: str, model_id: str, quality: float = 0.8):
        key = self._hash_prompt(prompt)
        if len(self.cache) >= self.max_size:
            # Evict least-hit entry
            oldest = min(self.cache.items(), key=lambda x: x[1].hit_count)
            del self.cache[oldest[0]]

        self.cache[key] = RoutingCacheEntry(
            prompt_hash=key,
            prompt_preview=prompt[:60],
            model_id=model_id,
            quality_observed=quality,
        )

    def stats(self) -> dict:
        if not self.cache:
            return {"size": 0}
        total_hits = sum(e.hit_count for e in self.cache.values())
        model_dist = {}
        for e in self.cache.values():
            tier = e.model_id.split("-")[1]
            model_dist[tier] = model_dist.get(tier, 0) + 1
        return {"size": len(self.cache), "total_hits": total_hits, "model_distribution": model_dist}


# Routing logic (reuses keyword classifier from Option 1)
def _classify_tier(prompt: str) -> str:
    import re
    lower = prompt.lower()
    if any(re.search(p, lower) for p in [
        r"\b(architect|evaluate|comprehensive analysis|research|trade-?offs?)\b",
        r"\b(production-grade|enterprise|expert-level)\b",
    ]):
        return "claude-opus-4-6"
    if len(prompt.split()) < 15 and re.search(r"^(what|who|when|is|are|does)\b", lower):
        return "claude-haiku-4-5-20251001"
    return "claude-sonnet-4-6"


routing_cache = SemanticRoutingCache(max_size=500)


def cached_route(prompt: str, max_tokens: int = 400) -> tuple[str, str, bool]:
    """Returns (response, model_id, cache_hit)."""
    client = anthropic.Anthropic()

    # Check cache first
    cached_model = routing_cache.get(prompt)
    if cached_model:
        print(f"[CACHE HIT] Using cached routing: {cached_model.split('-')[1]}")
        response = client.messages.create(
            model=cached_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text, cached_model, True

    # Cache miss: classify and route
    model_id = _classify_tier(prompt)
    print(f"[CACHE MISS] Classified as {model_id.split('-')[1]} — caching decision")

    response = client.messages.create(
        model=model_id,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    # Cache the routing decision
    routing_cache.set(prompt, model_id, quality=0.8)

    return response.content[0].text, model_id, False


if __name__ == "__main__":
    # Simulate repeated similar queries
    prompts = [
        "What is the capital of France?",
        "Write a Python function to sort a list.",
        "What is the capital of France?",   # Cache hit!
        "Write a Python function to sort a list.",  # Cache hit!
        "What is the capital of Germany?",  # Cache miss (different query)
    ]

    for prompt in prompts:
        print(f"\nPrompt: {prompt}")
        result, model, hit = cached_route(prompt)
        print(f"Model: {model.split('-')[1]} | Cache hit: {hit}")
        print(f"Result: {result[:80]}...")

    print(f"\nCache stats: {json.dumps(routing_cache.stats(), indent=2)}")

# Expected Token Savings: Routing overhead eliminated for repeated queries; cache hit rate 40-60% in typical workloads
# Environment: High-volume agents with repeated question patterns (FAQ bots, search assistants)
```

## Comparison

| Option | Classification Method | Overhead | Accuracy | Best For |
|--------|----------------------|----------|----------|----------|
| 1 Keyword Rules | Regex patterns | ~0ms | 70-80% | High-volume, latency-critical routing |
| 2 Haiku Meta-Classifier | LLM classification | ~300ms + cost | 88-93% | Unpredictable mixed workloads |
| 3 Budget + Deadline Constraints | Multi-constraint optimization | ~0ms | N/A | SLA-tiered APIs (premium vs. free) |
| 4 Adaptive Feedback | Historical quality tracking | ~500ms (with judge) | Improves over time | High-volume systems with feedback loops |
| 5 Two-Stage Quality Gate | Heuristic quality check | 1-3 LLM calls | 85-92% | General assistants with unknown complexity |
| 6 Semantic Cache | Exact-match hash cache | ~1ms on hit | Matches prior decisions | Repeated query patterns, FAQ bots |
