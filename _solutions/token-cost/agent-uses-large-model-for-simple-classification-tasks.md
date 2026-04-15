---
layout: solution
title: "Agent Uses Large Model for Simple Classification Tasks"
category: token-cost
description: "Agent routes all requests through the most capable (and expensive) model even when simple tasks like classification, extraction, or routing could be handled by a smaller, cheaper model."
tags: [token-cost, model-routing, haiku, classification, cost-optimization]
---

## Symptom

Every request — from "classify this as spam or not" to "extract the user's name from this sentence" — goes to `claude-opus-4-6` or `claude-sonnet-4-6`, costing 10-50× more than necessary. At scale, a system handling 100,000 classification requests per day on Opus costs roughly $15,000/month when Haiku would cost $300/month for the same work.

```python
# BROKEN: Opus for a yes/no classification
response = client.messages.create(
    model="claude-opus-4-6",     # $15/MTok input — overkill for "is this spam?"
    max_tokens=1024,
    messages=[{
        "role": "user",
        "content": f"Is this message spam? Answer yes or no.\n\nMessage: {user_message}"
    }]
)
```

Common tasks incorrectly sent to large models:
- Binary or multi-class text classification
- Intent detection (is this a question / complaint / request?)
- Entity extraction (names, dates, numbers)
- Language detection
- Sentiment analysis (positive / negative / neutral)
- Simple format conversion (JSON to CSV, markdown to text)
- Short factual lookups from provided context

---

## Root Cause

The default model in most agent setups is the most capable available, and engineers never revisit the choice as the system evolves. Simple sub-tasks that were once part of a complex pipeline get called with the same model as the pipeline itself. There's no routing logic — every call uses the same model regardless of complexity.

The fix is tiered routing: classify the task first (cheaply), then dispatch to the appropriate model tier.

---

## Fix

### Option 1 — Explicit Task Routing by Task Type

Define task categories and map them to appropriate models.

```python
import anthropic
from enum import Enum

client = anthropic.Anthropic()

class TaskComplexity(Enum):
    SIMPLE = "simple"       # classification, extraction, short lookups
    STANDARD = "standard"   # summarization, Q&A, code explanation
    COMPLEX = "complex"     # multi-step reasoning, code generation, analysis

MODEL_FOR_COMPLEXITY = {
    TaskComplexity.SIMPLE:   "claude-haiku-4-5-20251001",  # ~$0.25/MTok
    TaskComplexity.STANDARD: "claude-sonnet-4-6",           # ~$3/MTok
    TaskComplexity.COMPLEX:  "claude-opus-4-6",             # ~$15/MTok
}

MAX_TOKENS_FOR_COMPLEXITY = {
    TaskComplexity.SIMPLE:   50,
    TaskComplexity.STANDARD: 1024,
    TaskComplexity.COMPLEX:  4096,
}

def classify_task(task_description: str) -> TaskComplexity:
    """
    Classify task complexity using keyword heuristics.
    In production: use a lightweight classifier or small LLM.
    """
    task_lower = task_description.lower()

    simple_patterns = [
        "classify", "is this", "yes or no", "true or false",
        "extract the", "what is the sentiment", "detect the language",
        "which category", "spam or not", "positive or negative",
        "label this", "tag this", "is it a",
    ]
    complex_patterns = [
        "implement", "build", "design", "architect", "analyze in depth",
        "multi-step", "comprehensive", "research", "compare and contrast",
        "write a complete", "full implementation", "debug this complex",
    ]

    if any(p in task_lower for p in simple_patterns):
        return TaskComplexity.SIMPLE
    if any(p in task_lower for p in complex_patterns):
        return TaskComplexity.COMPLEX
    return TaskComplexity.STANDARD

def routed_call(
    task_description: str,
    prompt: str,
    force_complexity: TaskComplexity = None,
) -> dict:
    """Make an API call routed to the appropriate model tier."""
    complexity = force_complexity or classify_task(task_description)
    model = MODEL_FOR_COMPLEXITY[complexity]
    max_tokens = MAX_TOKENS_FOR_COMPLEXITY[complexity]

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "result": response.content[0].text,
        "model_used": model,
        "complexity": complexity.value,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }

# Test routing
tasks = [
    ("classify this email as spam or not spam", "Is this spam? Reply SPAM or NOT_SPAM only.\n\nEmail: Win $1000 now!!! Click here!!!"),
    ("summarize this article in 3 bullet points", "Summarize: The new API allows developers to build agents that can use tools, remember context, and collaborate with other agents."),
    ("implement a complete authentication system with JWT, refresh tokens, and rate limiting", "Design and implement a production-ready auth system..."),
]

total_cost_estimate = 0
for task_desc, prompt in tasks:
    result = routed_call(task_desc, prompt)
    # Rough cost estimate (input + output)
    model = result["model_used"]
    rate = {"claude-haiku-4-5-20251001": 0.00025, "claude-sonnet-4-6": 0.003, "claude-opus-4-6": 0.015}[model]
    cost = (result["input_tokens"] + result["output_tokens"]) / 1_000_000 * rate
    total_cost_estimate += cost
    print(f"Task: {task_desc[:50]}")
    print(f"  Model: {result['model_used']} | Complexity: {result['complexity']}")
    print(f"  Tokens: {result['input_tokens']}+{result['output_tokens']} | ~${cost:.6f}")
    print(f"  Result: {result['result'][:80]}\n")

print(f"Total estimated cost: ${total_cost_estimate:.4f}")
```

**Expected Token Savings:** 95% cost reduction on simple tasks. 100K daily classifications: Haiku at $62.50/day vs Opus at $3,750/day.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 2 — Input Length and Output Size Heuristics

Route based on measurable signals: input length, expected output size, and task structure.

```python
import anthropic
import re

client = anthropic.Anthropic()

def estimate_complexity(
    prompt: str,
    expected_output_tokens: int = None,
    has_code: bool = None,
    has_tools: bool = False,
) -> tuple[str, str]:
    """
    Estimate model and max_tokens from measurable prompt signals.
    Returns (model_id, reason).
    """
    input_tokens_estimate = len(prompt.split()) * 1.3  # rough token estimate

    # Detect code in prompt
    if has_code is None:
        has_code = bool(re.search(r'```|def |class |function |import |<\w+>', prompt))

    # Output size signals
    output_is_small = expected_output_tokens is not None and expected_output_tokens <= 100
    output_is_large = expected_output_tokens is not None and expected_output_tokens > 1000

    # Route logic
    if (
        input_tokens_estimate < 500
        and output_is_small
        and not has_code
        and not has_tools
    ):
        return "claude-haiku-4-5-20251001", "short input, small output, no code"

    if (
        input_tokens_estimate > 3000
        or output_is_large
        or has_tools
    ):
        return "claude-sonnet-4-6", "long input, large output, or tools required"

    # Default: standard tasks
    return "claude-sonnet-4-6", "standard complexity"

def smart_call(
    prompt: str,
    expected_output_words: int = None,
    has_tools: bool = False,
    tools: list = None,
) -> dict:
    expected_tokens = int(expected_output_words * 1.3) if expected_output_words else None
    model, reason = estimate_complexity(
        prompt, expected_tokens, has_tools=has_tools
    )
    max_tokens = min(max(expected_tokens or 256, 50), 4096)

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    if tools:
        kwargs["tools"] = tools

    response = client.messages.create(**kwargs)
    return {
        "text": response.content[0].text if response.content and hasattr(response.content[0], "text") else "",
        "model": model,
        "reason": reason,
        "tokens": response.usage.input_tokens + response.usage.output_tokens,
    }

# Test signal-based routing
examples = [
    {
        "prompt": "Classify sentiment: 'I love this product!' — Answer: POSITIVE, NEGATIVE, or NEUTRAL",
        "expected_output_words": 1,
        "desc": "Sentiment (1-word output)",
    },
    {
        "prompt": "Translate this to French: 'Hello, how are you today?'",
        "expected_output_words": 8,
        "desc": "Short translation",
    },
    {
        "prompt": "Write a comprehensive 1000-word technical blog post about transformer attention mechanisms, covering self-attention, multi-head attention, and positional encoding with examples.",
        "expected_output_words": 1000,
        "desc": "Long blog post",
    },
    {
        "prompt": "Extract all email addresses from: 'Contact us at info@example.com or support@company.org'",
        "expected_output_words": 5,
        "desc": "Entity extraction",
    },
]

for ex in examples:
    result = smart_call(ex["prompt"], ex.get("expected_output_words"))
    print(f"{ex['desc']}: {result['model'].split('-')[1]} ({result['reason']}) — {result['tokens']} tokens")
```

**Expected Token Savings:** Signal-based routing requires no extra LLM calls. Routes ~60% of typical workloads to Haiku, saving ~80% cost on those calls.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 3 — Haiku-First with Sonnet Escalation

Try Haiku first; if the response is insufficient, escalate to Sonnet automatically.

```python
import anthropic
import json

client = anthropic.Anthropic()

QUALITY_CHECK_PROMPT = """Rate this response quality on a scale of 1-5:
1 = Wrong/incoherent
2 = Partially correct or incomplete
3 = Adequate
4 = Good
5 = Excellent

Task: {task}
Response: {response}

Reply with just a single digit (1-5)."""

def haiku_first(
    prompt: str,
    task_description: str = "",
    quality_threshold: int = 3,
    max_tokens: int = 512,
) -> dict:
    """
    Try Haiku first. Escalate to Sonnet if quality check fails.
    """
    # Attempt 1: Haiku
    haiku_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    haiku_text = haiku_resp.content[0].text

    # Quality check using Haiku (cheap)
    quality_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{
            "role": "user",
            "content": QUALITY_CHECK_PROMPT.format(
                task=task_description or prompt[:100],
                response=haiku_text[:300]
            )
        }]
    )

    try:
        quality_score = int(quality_resp.content[0].text.strip()[0])
    except (ValueError, IndexError):
        quality_score = 3  # default to adequate if parse fails

    if quality_score >= quality_threshold:
        return {
            "result": haiku_text,
            "model_used": "claude-haiku-4-5-20251001",
            "escalated": False,
            "quality_score": quality_score,
            "haiku_tokens": haiku_resp.usage.input_tokens + haiku_resp.usage.output_tokens,
            "sonnet_tokens": 0,
        }

    # Escalate to Sonnet
    print(f"  Haiku quality {quality_score}/5 — escalating to Sonnet")
    sonnet_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "result": sonnet_resp.content[0].text,
        "model_used": "claude-sonnet-4-6",
        "escalated": True,
        "quality_score": quality_score,
        "haiku_tokens": haiku_resp.usage.input_tokens + haiku_resp.usage.output_tokens,
        "sonnet_tokens": sonnet_resp.usage.input_tokens + sonnet_resp.usage.output_tokens,
    }

# Test: simple tasks stay on Haiku; complex ones escalate
test_cases = [
    ("Is 'numpy' a Python or JavaScript library?", "Identify which language a library belongs to"),
    ("What is 7 × 8?", "Basic arithmetic"),
    ("Explain quantum entanglement and its implications for quantum computing", "Complex physics explanation"),
    ("Extract all phone numbers from: 'Call us at 555-1234 or 555-5678'", "Phone number extraction"),
]

for prompt, task_desc in test_cases:
    result = haiku_first(prompt, task_desc, quality_threshold=3)
    print(f"\nTask: {task_desc}")
    print(f"  Model: {result['model_used']} | Escalated: {result['escalated']} | Quality: {result['quality_score']}/5")
    print(f"  Result: {result['result'][:100]}")
```

**Expected Token Savings:** 70-80% of simple tasks complete on Haiku (10× cheaper). Quality check adds ~50 tokens but catches Haiku failures before they reach users.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 4 — Batch Classification with Haiku

Batch multiple simple classifications into a single Haiku call instead of one Sonnet call per item.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

def batch_classify(
    items: list[str],
    labels: list[str],
    instruction: str,
    batch_size: int = 20,
) -> list[str]:
    """
    Classify multiple items in batches using Haiku.
    Much cheaper than one Sonnet call per item.
    """
    all_results = []

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {item}" for j, item in enumerate(batch))

        prompt = (
            f"{instruction}\n\n"
            f"Valid labels: {', '.join(labels)}\n\n"
            f"Items to classify:\n{numbered}\n\n"
            f"Reply with a JSON array of labels, one per item, in order. "
            f"Example: [\"label1\", \"label2\", ...]\n"
            f"Reply with ONLY the JSON array, no other text."
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=len(batch) * 20,  # ~20 tokens per label
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text.strip()
        # Extract JSON array
        try:
            # Handle possible markdown fences
            if "```" in text:
                text = text.split("```")[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            batch_labels = json.loads(text)
            # Validate and normalize
            batch_labels = [
                label if label in labels else labels[0]
                for label in batch_labels[:len(batch)]
            ]
        except (json.JSONDecodeError, KeyError):
            # Fallback: one per line
            lines = [l.strip().strip('"').strip("'") for l in text.split("\n") if l.strip()]
            batch_labels = lines[:len(batch)] or [labels[0]] * len(batch)

        all_results.extend(batch_labels)
        print(f"  Batch {i//batch_size + 1}: classified {len(batch)} items")

    return all_results

# Test: classify 50 support tickets in bulk
ticket_samples = [
    "My order hasn't arrived after 2 weeks",
    "How do I change my password?",
    "The product is broken, I want a refund",
    "Do you ship to Canada?",
    "I was charged twice for the same order",
    "What are your business hours?",
    "I love this product, thank you!",
    "The website keeps crashing on mobile",
    "Can I return something I bought 6 months ago?",
    "You guys are the worst, I'm cancelling",
] * 5  # 50 tickets

labels = ["billing", "shipping", "returns", "technical", "general_inquiry", "complaint", "compliment"]

print(f"Classifying {len(ticket_samples)} tickets...")
results = batch_classify(
    ticket_samples, labels,
    "Classify each customer support ticket into exactly one category."
)

from collections import Counter
distribution = Counter(results)
print(f"\nClassification distribution:")
for label, count in distribution.most_common():
    print(f"  {label}: {count}")
print(f"\nTotal classified: {len(results)} tickets")
# One Haiku call per batch-of-20 vs 50 Sonnet calls: ~50× cost reduction
```

**Expected Token Savings:** Batching 20 items per call: 50 items = 3 Haiku calls vs 50 Sonnet calls. Cost reduction: ~97% (3 × $0.0001 vs 50 × $0.003).

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 5 — Pre-computed Classification Cache

Cache classification results so repeated identical inputs use no API calls at all.

```python
import anthropic
import hashlib
import sqlite3
import json
import time

client = anthropic.Anthropic()

class ClassificationCache:
    """SQLite-backed cache for classification results."""

    def __init__(self, db_path: str = ":memory:", ttl_seconds: int = 86400):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.ttl = ttl_seconds
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                model TEXT NOT NULL,
                cached_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0
            );
        """)
        self.db.commit()
        self.hits = 0
        self.misses = 0

    def _key(self, prompt: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()

    def get(self, prompt: str, model: str) -> str | None:
        key = self._key(prompt, model)
        row = self.db.execute(
            "SELECT result, cached_at FROM cache WHERE key=?", (key,)
        ).fetchone()

        if row:
            result, cached_at = row
            if time.time() - cached_at < self.ttl:
                self.db.execute(
                    "UPDATE cache SET hit_count=hit_count+1 WHERE key=?", (key,)
                )
                self.db.commit()
                self.hits += 1
                return result

        self.misses += 1
        return None

    def set(self, prompt: str, model: str, result: str):
        key = self._key(prompt, model)
        self.db.execute(
            "INSERT OR REPLACE INTO cache (key, result, model, cached_at) VALUES (?,?,?,?)",
            (key, result, model, time.time())
        )
        self.db.commit()

    def stats(self) -> dict:
        total = self.hits + self.misses
        row = self.db.execute("SELECT COUNT(*), SUM(hit_count) FROM cache").fetchone()
        return {
            "cache_entries": row[0],
            "total_hits": self.hits,
            "total_misses": self.misses,
            "hit_rate": self.hits / max(total, 1),
            "api_calls_saved": self.hits,
        }

cache = ClassificationCache(ttl_seconds=3600)

def cached_classify(text: str, labels: list[str], instruction: str) -> dict:
    """Classify with caching — repeated inputs never hit the API."""
    prompt = f"{instruction}\nLabels: {', '.join(labels)}\nText: {text}\nReply with label only."
    model = "claude-haiku-4-5-20251001"

    cached = cache.get(prompt, model)
    if cached:
        return {"result": cached, "from_cache": True, "model": model}

    response = client.messages.create(
        model=model,
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}]
    )
    result = response.content[0].text.strip()
    cache.set(prompt, model, result)
    return {"result": result, "from_cache": False, "model": model}

# Simulate production traffic — many repeated classifications
LABELS = ["positive", "negative", "neutral"]
INSTRUCTION = "Classify the sentiment of this text."

sample_texts = [
    "I love this product!",
    "This is terrible",
    "It's okay I guess",
    "I love this product!",   # repeat — cache hit
    "This is terrible",       # repeat — cache hit
    "Amazing quality!",
    "I love this product!",   # repeat — cache hit
    "Worst purchase ever",
    "I love this product!",   # repeat — cache hit
]

print("Processing with cache:\n")
for text in sample_texts:
    result = cached_classify(text, LABELS, INSTRUCTION)
    cache_marker = "[CACHE]" if result["from_cache"] else "[API]  "
    print(f"  {cache_marker} '{text[:40]}' → {result['result']}")

stats = cache.stats()
print(f"\nCache stats: {stats['hit_rate']:.0%} hit rate | {stats['api_calls_saved']} API calls saved")
```

**Expected Token Savings:** Cache eliminates API costs entirely on repeated inputs. Support bot handling 10,000 daily queries with 40% repeat rate: saves 4,000 API calls/day.

**Environment:** Python 3.9+, `sqlite3`, `anthropic>=0.40.0`.

---

### Option 6 — Cost Budget Enforcer with Model Downgrade

Track token costs per session and automatically downgrade models when budget thresholds are reached.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

# Token costs per million tokens (input/output)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25,  "output": 1.25},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class BudgetTracker:
    session_budget_usd: float
    spent_usd: float = 0.0
    call_count: int = 0
    downgrade_log: list[str] = field(default_factory=list)

    def record_call(self, model: str, input_tokens: int, output_tokens: int) -> float:
        costs = MODEL_COSTS.get(model, MODEL_COSTS["claude-sonnet-4-6"])
        cost = (input_tokens / 1_000_000 * costs["input"] +
                output_tokens / 1_000_000 * costs["output"])
        self.spent_usd += cost
        self.call_count += 1
        return cost

    def remaining_budget(self) -> float:
        return max(0.0, self.session_budget_usd - self.spent_usd)

    def budget_pct_used(self) -> float:
        return self.spent_usd / self.session_budget_usd

    def recommended_model(
        self,
        preferred: str,
        task_critical: bool = False,
    ) -> str:
        """
        Return the recommended model given current budget state.
        Downgrades non-critical tasks when budget is tight.
        """
        pct = self.budget_pct_used()

        if task_critical:
            return preferred  # never downgrade critical tasks

        if pct > 0.90:
            # Budget nearly exhausted — use Haiku for everything
            if preferred != "claude-haiku-4-5-20251001":
                self.downgrade_log.append(f"Downgraded {preferred} → haiku (budget {pct:.0%} used)")
            return "claude-haiku-4-5-20251001"

        if pct > 0.70 and preferred == "claude-opus-4-6":
            # Budget stressed — downgrade Opus to Sonnet
            self.downgrade_log.append(f"Downgraded opus → sonnet (budget {pct:.0%} used)")
            return "claude-sonnet-4-6"

        return preferred

budget = BudgetTracker(session_budget_usd=0.10)  # $0.10 session budget

def budget_aware_call(
    messages: list[dict],
    preferred_model: str = "claude-sonnet-4-6",
    task_critical: bool = False,
    max_tokens: int = 512,
) -> dict:
    """Make API call with automatic model downgrade when budget is tight."""
    effective_model = budget.recommended_model(preferred_model, task_critical)

    if effective_model != preferred_model:
        print(f"  [budget] Downgraded {preferred_model} → {effective_model} "
              f"(spent ${budget.spent_usd:.4f}/${budget.session_budget_usd:.2f})")

    response = client.messages.create(
        model=effective_model,
        max_tokens=max_tokens,
        messages=messages,
    )

    cost = budget.record_call(
        effective_model,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    return {
        "text": response.content[0].text,
        "model": effective_model,
        "cost_usd": cost,
        "budget_remaining": budget.remaining_budget(),
    }

# Simulate a session with many calls
queries = [
    ("Is this spam? 'Win $1000 NOW!'", False),
    ("Explain the CAP theorem in distributed systems", False),
    ("URGENT: Is this a security vulnerability? " + "A"*200, True),  # critical
    ("Summarize this in 2 sentences: " + "B"*500, False),
    ("What is 2 + 2?", False),
    ("Translate to Spanish: 'Good morning'", False),
]

for query, is_critical in queries:
    result = budget_aware_call(
        [{"role": "user", "content": query}],
        preferred_model="claude-sonnet-4-6",
        task_critical=is_critical,
    )
    print(f"  Model: {result['model']} | Cost: ${result['cost_usd']:.5f} | "
          f"Remaining: ${result['budget_remaining']:.4f}")
    print(f"  Response: {result['text'][:80]}\n")

print(f"\nTotal calls: {budget.call_count}")
print(f"Total spent: ${budget.spent_usd:.4f}")
print(f"Downgrades: {budget.downgrade_log}")
```

**Expected Token Savings:** Budget-based downgrading ensures cost stays within bounds while preserving quality for critical tasks. Typical savings: 40-60% on mixed-criticality workloads.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

## Comparison

| Option | Routing Method | Extra LLM Calls | Adapts at Runtime | Batch Support |
|--------|--------------|----------------|------------------|---------------|
| 1 — Task Type Routing | Keyword heuristic | None | No | No |
| 2 — Signal-Based | Input/output metrics | None | No | No |
| 3 — Haiku-First Escalation | Quality check | +1 Haiku/call | No | No |
| 4 — Batch Classification | None | None | No | Yes |
| 5 — Classification Cache | None | None | Yes (cache) | No |
| 6 — Budget Enforcer | Spend tracking | None | Yes | No |

**Start with Option 1** (task type routing) for immediate 80% cost reduction on classification workloads. Add **Option 5** (caching) for any repeated classification patterns. Use **Option 6** (budget enforcer) in production to prevent runaway costs during traffic spikes.
