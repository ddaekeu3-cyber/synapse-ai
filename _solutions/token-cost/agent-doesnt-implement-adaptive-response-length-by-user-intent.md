---
title: "Agent Doesn't Implement Adaptive Response Length by User Intent"
description: "Detect user intent and dynamically set max_tokens to match—brief for quick lookups, detailed for explanations, exhaustive for research—cutting wasted tokens and improving response quality simultaneously."
difficulty: intermediate
category: token-cost
tags: [token-cost, response-length, intent-detection, adaptive, cost-optimization]
---

## Problem

Agents use a single fixed `max_tokens` value for all requests. A user asking "What is 2+2?" gets the same token budget as one asking for a comprehensive architecture review. This wastes tokens (and money) on simple queries, while under-provisioning complex ones that get cut off mid-sentence. Adaptive length matching reduces costs by 30-70% while improving quality on complex requests.

## Solutions

### Option 1: Keyword-Based Intent Classifier

Use fast pattern matching to detect intent before sending to the model.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class IntentConfig:
    name: str
    max_tokens: int
    description: str

INTENT_CONFIGS = {
    "factual_quick": IntentConfig("factual_quick", 80, "Single-fact lookups"),
    "definition": IntentConfig("definition", 150, "Define/explain a concept"),
    "how_to": IntentConfig("how_to", 400, "Step-by-step instructions"),
    "code": IntentConfig("code", 600, "Code generation or review"),
    "analysis": IntentConfig("analysis", 800, "Deep analysis or comparison"),
    "research": IntentConfig("research", 1200, "Comprehensive research"),
}

INTENT_PATTERNS = {
    "factual_quick": [
        r"^(what is|who is|when did|where is|how many|what's) .{0,40}\?$",
        r"^(define|meaning of) ",
        r"^\w+\?$",
    ],
    "definition": [
        r"explain (what|how|why)",
        r"what does .+ mean",
        r"(describe|define) .{10,}",
    ],
    "how_to": [
        r"how (do|can|should) (i|we|you)",
        r"(steps|guide|tutorial|walk me through)",
        r"how to ",
    ],
    "code": [
        r"(write|implement|create|build|fix|debug|refactor) .*(code|function|class|script|method)",
        r"```",
        r"(python|javascript|typescript|rust|go|java)\s+(code|example|snippet)",
    ],
    "analysis": [
        r"(compare|contrast|analyze|evaluate|pros and cons|trade-?offs)",
        r"(what are the (advantages|disadvantages|differences|similarities))",
        r"(review|assess|critique)",
    ],
    "research": [
        r"(comprehensive|exhaustive|complete|thorough|in-depth)",
        r"(research|survey|overview of) ",
        r"tell me everything about",
    ],
}

def classify_intent(query: str) -> IntentConfig:
    query_lower = query.lower().strip()

    for intent, patterns in INTENT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, query_lower):
                return INTENT_CONFIGS[intent]

    # Default: moderate length
    return INTENT_CONFIGS["how_to"]

async def adaptive_complete(query: str) -> tuple[str, IntentConfig]:
    intent = classify_intent(query)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=intent.max_tokens,
        messages=[{"role": "user", "content": query}]
    )

    return response.content[0].text, intent

async def demo_keyword_classifier():
    queries = [
        "What is the capital of France?",
        "How do I center a div in CSS?",
        "Write a Python function to parse JSON safely.",
        "Compare REST vs GraphQL APIs.",
        "Explain what recursion is.",
        "Give me a comprehensive overview of distributed systems architectures.",
    ]

    total_tokens = 0
    for query in queries:
        result, intent = await adaptive_complete(query)
        tokens_used = len(result.split())  # Approximate
        total_tokens += tokens_used
        print(f"\n[{intent.name} | max={intent.max_tokens}] {query[:50]}")
        print(f"  Response ({tokens_used} words): {result.strip()[:100]}...")

    print(f"\nTotal approx tokens used: {total_tokens}")

asyncio.run(demo_keyword_classifier())
```

### Option 2: LLM-Based Intent Classification with Budget Mapping

Use a fast, cheap model to classify intent and map it to a token budget.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CLASSIFIER_SYSTEM = """Classify the user's query intent and return JSON only.

Return: {"intent": "...", "max_tokens": N, "reason": "..."}

Intent options and token budgets:
- "greeting": 30 tokens (hello, hi, thanks)
- "yes_no": 50 tokens (yes/no questions)
- "quick_fact": 80 tokens (single fact lookup)
- "explanation": 200 tokens (explain a concept)
- "how_to": 400 tokens (step-by-step guide)
- "code_generation": 700 tokens (write code)
- "detailed_analysis": 1000 tokens (compare, analyze, review)
- "comprehensive_research": 1500 tokens (exhaustive coverage)

Return ONLY the JSON object."""

async def classify_query_budget(query: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # Cheapest model for classification
        max_tokens=100,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": f"Query: {query}"}]
    )
    text = response.content[0].text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"intent": "how_to", "max_tokens": 400, "reason": "default"}

async def smart_complete(query: str) -> dict:
    # Step 1: classify (cheap, fast)
    budget = await classify_query_budget(query)
    max_tokens = budget["max_tokens"]

    # Step 2: answer with appropriate budget
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": query}]
    )
    answer = response.content[0].text

    return {
        "query": query,
        "intent": budget["intent"],
        "allocated_tokens": max_tokens,
        "actual_tokens": response.usage.output_tokens,
        "efficiency": f"{response.usage.output_tokens / max_tokens * 100:.0f}% of budget used",
        "answer_preview": answer.strip()[:100],
    }

async def demo_llm_classifier():
    queries = [
        "Hi!",
        "Is Python interpreted?",
        "What is a neural network?",
        "How do I set up a FastAPI server?",
        "Write a binary search implementation in Python.",
        "Compare PostgreSQL vs MongoDB for a social network app.",
    ]

    results = await asyncio.gather(*[smart_complete(q) for q in queries])

    total_allocated = sum(r["allocated_tokens"] for r in results)
    total_used = sum(r["actual_tokens"] for r in results)
    fixed_budget = 800 * len(queries)  # What a fixed-max_tokens=800 would cost

    print(f"{'Query':<45} {'Intent':<22} {'Alloc':>6} {'Used':>6}")
    print("-" * 85)
    for r in results:
        print(f"{r['query'][:44]:<45} {r['intent']:<22} {r['allocated_tokens']:>6} {r['actual_tokens']:>6}")

    print(f"\nTotal tokens — adaptive: {total_used} | fixed-800: {fixed_budget}")
    print(f"Token savings: {(1 - total_used/fixed_budget)*100:.0f}%")

asyncio.run(demo_llm_classifier())
```

### Option 3: Message History Length Heuristic

Infer response depth from conversation context—later turns typically need shorter responses.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class ConversationTokenStrategy:
    """Dynamically adjust max_tokens based on conversation state."""

    base_max_tokens: int = 500
    min_tokens: int = 50
    max_tokens: int = 1500

    def calculate_budget(
        self,
        current_message: str,
        history: list[dict],
        is_continuation: bool = False,
    ) -> int:
        budget = self.base_max_tokens

        # Factor 1: Message length (longer query → longer answer expected)
        msg_words = len(current_message.split())
        if msg_words < 5:
            budget = int(budget * 0.3)
        elif msg_words < 15:
            budget = int(budget * 0.7)
        elif msg_words > 50:
            budget = int(budget * 1.8)

        # Factor 2: Question type markers
        msg_lower = current_message.lower()
        if any(w in msg_lower for w in ["briefly", "quick", "short", "tldr", "one sentence"]):
            budget = min(budget, 80)
        elif any(w in msg_lower for w in ["comprehensive", "detailed", "exhaustive", "thorough"]):
            budget = max(budget, 1000)
        elif any(w in msg_lower for w in ["and", "also", "furthermore", "additionally"]):
            budget = int(budget * 1.3)  # Compound question

        # Factor 3: Conversation position
        if len(history) > 10:
            # Deep in conversation: likely refinement, not first explanation
            budget = int(budget * 0.7)
        elif is_continuation:
            budget = int(budget * 0.5)

        return max(self.min_tokens, min(self.max_tokens, budget))

class AdaptiveConversationAgent:
    def __init__(self):
        self.strategy = ConversationTokenStrategy()
        self.history: list[dict] = []

    async def chat(self, user_message: str) -> str:
        is_continuation = (
            self.history and
            self.history[-1]["role"] == "assistant" and
            len(user_message.split()) < 10
        )

        max_tokens = self.strategy.calculate_budget(
            user_message, self.history, is_continuation
        )

        self.history.append({"role": "user", "content": user_message})

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=self.history,
        )
        text = response.content[0].text
        self.history.append({"role": "assistant", "content": text})

        actual = response.usage.output_tokens
        print(f"  Budget: {max_tokens} | Used: {actual} | Words in Q: {len(user_message.split())}")
        return text

async def demo_heuristic_strategy():
    agent = AdaptiveConversationAgent()

    conversation = [
        "Hi!",
        "What is asyncio?",
        "Give me a comprehensive guide to using asyncio with the Anthropic API, including error handling and retry logic.",
        "Can you briefly summarize that?",
        "And what about rate limiting specifically?",
        "Thanks!",
    ]

    for msg in conversation:
        print(f"\nUser: {msg}")
        response = await agent.chat(msg)
        print(f"Agent: {response.strip()[:100]}...")

asyncio.run(demo_heuristic_strategy())
```

### Option 4: Per-Endpoint Token Budget Configuration

Expose intent-to-budget mappings as configuration so they can be tuned without code changes.

```python
import asyncio
import json
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from pathlib import Path

client = AsyncAnthropic()

DEFAULT_BUDGET_CONFIG = {
    "endpoints": {
        "chat": {
            "default_max_tokens": 400,
            "intent_overrides": {
                "quick_question": 80,
                "explanation": 300,
                "code": 600,
                "analysis": 900,
            }
        },
        "search": {
            "default_max_tokens": 200,
            "intent_overrides": {
                "snippet": 100,
                "summary": 250,
            }
        },
        "document_qa": {
            "default_max_tokens": 600,
            "intent_overrides": {
                "yes_no": 60,
                "extract": 200,
                "synthesize": 800,
            }
        }
    },
    "global_max": 2000,
    "global_min": 30,
}

@dataclass
class BudgetConfig:
    config: dict = field(default_factory=lambda: DEFAULT_BUDGET_CONFIG)

    def get_budget(self, endpoint: str, intent: str) -> int:
        ep_config = self.config["endpoints"].get(endpoint, {})
        budget = ep_config.get("intent_overrides", {}).get(
            intent,
            ep_config.get("default_max_tokens", 400)
        )
        return max(
            self.config["global_min"],
            min(self.config["global_max"], budget)
        )

    def save(self, path: Path):
        path.write_text(json.dumps(self.config, indent=2))

    @classmethod
    def load(cls, path: Path) -> "BudgetConfig":
        if path.exists():
            return cls(config=json.loads(path.read_text()))
        return cls()

def detect_intent(query: str) -> str:
    lower = query.lower()
    if re.search(r"\b(yes|no|is|are|does|did|can|will)\b.{0,30}\?$", lower):
        return "yes_no"
    elif re.search(r"(code|function|implement|write)", lower):
        return "code"
    elif re.search(r"(analyze|compare|evaluate|pros|cons)", lower):
        return "analysis"
    elif len(query.split()) < 8:
        return "quick_question"
    else:
        return "explanation"

async def endpoint_aware_complete(
    query: str,
    endpoint: str,
    budget_config: BudgetConfig,
) -> dict:
    intent = detect_intent(query)
    max_tokens = budget_config.get_budget(endpoint, intent)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": query}]
    )

    return {
        "endpoint": endpoint,
        "intent": intent,
        "max_tokens": max_tokens,
        "used_tokens": response.usage.output_tokens,
        "answer": response.content[0].text.strip()[:80],
    }

async def demo_endpoint_budgets():
    config = BudgetConfig()

    test_cases = [
        ("What is Python?", "chat"),
        ("Is asyncio thread-safe?", "document_qa"),
        ("Write a retry decorator.", "chat"),
        ("Compare async and threading.", "chat"),
        ("Summary of this doc.", "search"),
    ]

    results = await asyncio.gather(*[
        endpoint_aware_complete(q, ep, config) for q, ep in test_cases
    ])

    print(f"{'Query':<35} {'Endpoint':<12} {'Intent':<16} {'Budget':>7} {'Used':>6}")
    print("-" * 80)
    for r in results:
        print(f"{r['endpoint']:<12} {r['intent']:<16} {r['max_tokens']:>7} {r['used_tokens']:>6}")

asyncio.run(demo_endpoint_budgets())
```

### Option 5: Dynamic Budget Based on Response Confidence

Start with a short budget, detect if the response was truncated, and re-request with more tokens.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

TRUNCATION_SIGNALS = [
    "...",
    "to be continued",
    "in conclusion",  # Abrupt conclusion mid-thought
]

def looks_truncated(text: str, stop_reason: str) -> bool:
    """Detect if response was cut off prematurely."""
    if stop_reason == "max_tokens":
        last_char = text.rstrip()[-1] if text.strip() else ""
        # Truncated if ends mid-sentence (no terminal punctuation)
        return last_char not in ".!?)]}"
    return False

async def elastic_complete(
    query: str,
    initial_budget: int = 150,
    max_budget: int = 1200,
    expansion_factor: float = 2.5,
) -> tuple[str, int, int]:
    """Complete with automatic budget expansion if truncated."""
    budget = initial_budget
    expansions = 0

    while budget <= max_budget:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=budget,
            messages=[{"role": "user", "content": query}]
        )
        text = response.content[0].text
        stop_reason = response.stop_reason

        if not looks_truncated(text, stop_reason):
            return text, budget, expansions

        # Expand budget and retry
        new_budget = min(int(budget * expansion_factor), max_budget)
        if new_budget == budget:
            break  # At max, return what we have
        budget = new_budget
        expansions += 1
        print(f"  Response truncated. Expanding budget to {budget} tokens...")

    return text, budget, expansions

async def demo_elastic_budget():
    test_queries = [
        ("What is 2+2?", 150),
        ("List 10 Python best practices with brief explanations.", 100),  # Will expand
        ("Explain async/await in one sentence.", 150),
        ("Write a complete implementation of a LRU cache in Python with all methods.", 80),  # Will expand
    ]

    for query, initial in test_queries:
        print(f"\nQ: {query[:60]}")
        result, final_budget, expansions = await elastic_complete(
            query, initial_budget=initial
        )
        print(f"  Budget: {initial} → {final_budget} ({expansions} expansions)")
        print(f"  Answer: {result.strip()[:120]}...")

asyncio.run(demo_elastic_budget())
```

### Option 6: User Preference Learning for Budget Personalization

Track each user's preferences for response length and personalize budgets over time.

```python
import asyncio
import json
from pathlib import Path
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()
PREFS_FILE = Path(".user_length_prefs.json")

@dataclass
class UserLengthProfile:
    user_id: str
    preferred_length: str = "medium"  # "brief", "medium", "detailed"
    satisfaction_by_length: dict = field(default_factory=lambda: {
        "brief": [], "medium": [], "detailed": []
    })

    LENGTH_BUDGETS = {
        "brief": 120,
        "medium": 350,
        "detailed": 900,
    }

    def budget_for_intent(self, intent: str) -> int:
        base = self.LENGTH_BUDGETS[self.preferred_length]
        # Adjust for intent
        if "code" in intent:
            return int(base * 1.8)
        elif "quick" in intent or "yes_no" in intent:
            return max(60, int(base * 0.3))
        return base

    def record_feedback(self, length_used: str, satisfied: bool):
        self.satisfaction_by_length[length_used].append(1 if satisfied else 0)
        # Adapt preference based on satisfaction history
        best_length = max(
            self.LENGTH_BUDGETS.keys(),
            key=lambda l: (
                sum(self.satisfaction_by_length[l]) /
                max(len(self.satisfaction_by_length[l]), 1)
            )
        )
        if len(self.satisfaction_by_length[best_length]) >= 3:
            self.preferred_length = best_length

class PersonalizedAgent:
    def __init__(self):
        self._profiles: dict[str, UserLengthProfile] = {}
        self._load_profiles()

    def _load_profiles(self):
        if PREFS_FILE.exists():
            data = json.loads(PREFS_FILE.read_text())
            for uid, p in data.items():
                profile = UserLengthProfile(user_id=uid)
                profile.preferred_length = p.get("preferred_length", "medium")
                profile.satisfaction_by_length = p.get("satisfaction", {
                    "brief": [], "medium": [], "detailed": []
                })
                self._profiles[uid] = profile

    def _save_profiles(self):
        data = {
            uid: {
                "preferred_length": p.preferred_length,
                "satisfaction": p.satisfaction_by_length,
            }
            for uid, p in self._profiles.items()
        }
        PREFS_FILE.write_text(json.dumps(data, indent=2))

    def get_profile(self, user_id: str) -> UserLengthProfile:
        if user_id not in self._profiles:
            self._profiles[user_id] = UserLengthProfile(user_id=user_id)
        return self._profiles[user_id]

    async def respond(self, user_id: str, query: str, intent: str = "general") -> dict:
        profile = self.get_profile(user_id)
        max_tokens = profile.budget_for_intent(intent)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": query}]
        )
        text = response.content[0].text

        return {
            "answer": text,
            "user_id": user_id,
            "preferred_length": profile.preferred_length,
            "tokens_allocated": max_tokens,
            "tokens_used": response.usage.output_tokens,
        }

    def record_feedback(self, user_id: str, satisfied: bool):
        profile = self.get_profile(user_id)
        profile.record_feedback(profile.preferred_length, satisfied)
        self._save_profiles()
        print(f"[Profile] {user_id} prefers '{profile.preferred_length}' responses "
              f"(updated from feedback)")

async def demo_personalized_budgets():
    agent = PersonalizedAgent()

    # User A: prefers brief answers
    agent.get_profile("user-A").preferred_length = "brief"
    # User B: prefers detailed answers
    agent.get_profile("user-B").preferred_length = "detailed"

    query = "What is dependency injection?"

    for user_id in ["user-A", "user-B"]:
        result = await agent.respond(user_id, query, intent="explanation")
        print(f"\n[{user_id} | {result['preferred_length']}]")
        print(f"  Budget: {result['tokens_allocated']} | Used: {result['tokens_used']}")
        print(f"  Answer: {result['answer'].strip()[:120]}...")

    # Simulate feedback
    agent.record_feedback("user-A", satisfied=True)   # Brief was good
    agent.record_feedback("user-B", satisfied=True)   # Detailed was good

asyncio.run(demo_personalized_budgets())
```

## Comparison

| Approach | Classification Speed | Accuracy | Personalization | Implementation |
|---|---|---|---|---|
| Keyword Pattern Matching | Instant (~0ms) | Medium | None | Low |
| LLM-Based Classification | ~200ms extra | High | None | Low |
| Message History Heuristic | Instant | Medium | Implicit | Low |
| Per-Endpoint Config | Instant | Medium-High | By endpoint | Medium |
| Elastic Budget Expansion | Adds retries | High (self-correcting) | None | Medium |
| User Preference Learning | Instant | Personalized | Full | Medium |

**Choose Keyword Pattern Matching** as an immediate zero-latency win—implement it in an afternoon and cut token costs by 30-50%. **Choose LLM-Based Classification** when query intent is nuanced and keyword patterns produce too many false positives. **Choose Elastic Budget Expansion** when you'd rather expand on demand than over-provision upfront—it's the safest default since responses are never cut off. **Choose User Preference Learning** for consumer products where different users genuinely prefer different verbosity levels.
