---
title: "Agent doesn't implement prompt routing by intent classifier"
description: "Every user message goes to the same large, expensive model with the same system prompt. Simple requests that need a one-word answer consume the same tokens as complex reasoning tasks, inflating cost and latency."
difficulty: intermediate
category: prompt-engineering
tags: [intent-classification, model-routing, prompt-routing, cost-optimization, multi-model]
---

## Problem

A single-model agent sends every request — "What time is it in Tokyo?", "Summarize this 50-page contract", "Debug this Python traceback" — through the same pipeline: the same system prompt, the same `claude-sonnet-4-6`, the same max_tokens budget. This is expensive and slow because:

1. Simple factual queries don't need a large reasoning model
2. Creative tasks need temperature > 0; classification tasks need temperature = 0
3. Code tasks benefit from code-focused prompting, not customer-service prompting
4. Short answers don't need a large token budget

Prompt routing classifies each incoming message and dispatches it to the optimal model, prompt template, and parameter set.

```python
# BAD: every request treated identically
async def agent(message: str) -> str:
    return await client.messages.create(
        model="claude-sonnet-4-6",  # always large, always expensive
        system="You are a helpful assistant.",  # always the same
        messages=[{"role": "user", "content": message}],
        max_tokens=4096,  # always maximum
    )
```

## Solution 1: Rule-based keyword router

Fast, deterministic routing based on keyword matching and heuristics. Zero API cost, sub-millisecond latency.

```python
import re
from dataclasses import dataclass
from typing import Literal


ModelTier = Literal[
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]


@dataclass
class RouteConfig:
    model: ModelTier
    system_prompt: str
    max_tokens: int
    temperature: float
    intent: str


ROUTES: dict[str, RouteConfig] = {
    "code": RouteConfig(
        model="claude-sonnet-4-6",
        system_prompt="You are an expert software engineer. Provide concise, correct code with brief explanations.",
        max_tokens=4096,
        temperature=0.0,
        intent="code",
    ),
    "summarize": RouteConfig(
        model="claude-sonnet-4-6",
        system_prompt="You are a precise summarizer. Extract key information and present it clearly.",
        max_tokens=2048,
        temperature=0.3,
        intent="summarize",
    ),
    "creative": RouteConfig(
        model="claude-sonnet-4-6",
        system_prompt="You are a creative writer with a vivid imagination.",
        max_tokens=8192,
        temperature=0.9,
        intent="creative",
    ),
    "factual": RouteConfig(
        model="claude-haiku-4-5-20251001",
        system_prompt="Answer factual questions accurately and concisely.",
        max_tokens=512,
        temperature=0.0,
        intent="factual",
    ),
    "analysis": RouteConfig(
        model="claude-opus-4-6",
        system_prompt="You are a rigorous analytical thinker. Reason step by step.",
        max_tokens=8192,
        temperature=0.2,
        intent="analysis",
    ),
    "default": RouteConfig(
        model="claude-sonnet-4-6",
        system_prompt="You are a helpful assistant.",
        max_tokens=4096,
        temperature=0.7,
        intent="default",
    ),
}

CODE_PATTERNS = re.compile(
    r"\b(debug|code|implement|function|class|error|traceback|syntax|python|javascript|typescript|sql|bug|refactor)\b",
    re.IGNORECASE,
)
SUMMARIZE_PATTERNS = re.compile(
    r"\b(summarize|summary|tldr|brief|overview|key points|main points)\b",
    re.IGNORECASE,
)
CREATIVE_PATTERNS = re.compile(
    r"\b(write a|story|poem|creative|imagine|fiction|narrative|essay)\b",
    re.IGNORECASE,
)
FACTUAL_PATTERNS = re.compile(
    r"\b(what is|who is|when did|where is|how many|what time|define|capital of)\b",
    re.IGNORECASE,
)
ANALYSIS_PATTERNS = re.compile(
    r"\b(analyze|evaluate|compare|pros and cons|trade-offs|deep dive|assess|strategy)\b",
    re.IGNORECASE,
)


def route_by_keywords(message: str) -> RouteConfig:
    if CODE_PATTERNS.search(message):
        return ROUTES["code"]
    if SUMMARIZE_PATTERNS.search(message):
        return ROUTES["summarize"]
    if CREATIVE_PATTERNS.search(message):
        return ROUTES["creative"]
    if FACTUAL_PATTERNS.search(message) and len(message) < 200:
        return ROUTES["factual"]
    if ANALYSIS_PATTERNS.search(message):
        return ROUTES["analysis"]
    return ROUTES["default"]


# ── Usage ────────────────────────────────────────────────────────────
messages = [
    "What is the capital of France?",
    "Debug this Python traceback: AttributeError on line 42",
    "Write a short poem about distributed systems",
    "Analyze the trade-offs between microservices and monoliths",
]
for msg in messages:
    route = route_by_keywords(msg)
    print(f"[{route.intent}] {route.model} → {msg[:50]}")
```

## Solution 2: LLM-based intent classifier using haiku

Use a small, cheap model to classify intent before routing to the appropriate model. More accurate than keyword matching; costs ~10x less than running the full model on every request.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

CLASSIFIER_SYSTEM = """Classify the user message into exactly one intent category.

Categories:
- code: programming, debugging, code review, technical implementation
- summarize: summarization, TLDR, overview, key points extraction
- creative: creative writing, storytelling, brainstorming, imaginative tasks
- factual: factual questions, definitions, simple lookups
- analysis: deep analysis, comparison, evaluation, strategy, complex reasoning
- default: anything else

Respond ONLY with a JSON object: {"intent": "<category>", "confidence": 0.0-1.0}"""


async def classify_intent(message: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        temperature=0.0,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": message[:1000]}],  # truncate for speed
    )
    text = response.content[0].text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"intent": "default", "confidence": 0.5}


# ── Route table (reuse from Solution 1) ──────────────────────────────
async def routed_agent(message: str) -> str:
    # Classify with haiku (~10ms, ~20 tokens)
    classification = await classify_intent(message)
    intent = classification["intent"]
    confidence = classification["confidence"]

    route = ROUTES.get(intent, ROUTES["default"])

    # Fall back to default if classifier is uncertain
    if confidence < 0.6:
        route = ROUTES["default"]

    print(f"Routed to [{route.intent}] via {route.model} (confidence={confidence:.0%})")

    response = await client.messages.create(
        model=route.model,
        max_tokens=route.max_tokens,
        temperature=route.temperature,
        system=route.system_prompt,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    result = await routed_agent("What is the capital of Japan?")
    print(result[:100])


asyncio.run(main())
```

## Solution 3: Embedding-based semantic router with pre-computed intent clusters

Embed the user message and compute cosine similarity against pre-embedded intent exemplars. No LLM call needed for classification; sub-millisecond routing after one-time setup.

```python
import asyncio
import numpy as np
from anthropic import AsyncAnthropic

client = AsyncAnthropic()


async def embed(text: str) -> np.ndarray:
    """
    Production: use voyage-3, text-embedding-3-small, or similar.
    Here: character n-gram pseudo-embedding for illustration.
    """
    import hashlib
    dim = 256
    vec = np.zeros(dim)
    for i in range(len(text) - 2):
        h = int(hashlib.md5(text[i:i+3].lower().encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# Intent exemplars — representative sentences for each category
EXEMPLARS: dict[str, list[str]] = {
    "code": [
        "Fix this Python bug", "Implement a binary search", "Review my JavaScript code",
        "Debug this traceback", "Write a SQL query",
    ],
    "summarize": [
        "Summarize this article", "Give me a TLDR", "What are the key points?",
        "Brief overview please", "Main takeaways from this document",
    ],
    "creative": [
        "Write a short story", "Compose a haiku", "Brainstorm startup ideas",
        "Create a fictional world", "Write a marketing tagline",
    ],
    "factual": [
        "What is the capital of France?", "Who invented the telephone?",
        "How many planets are in the solar system?", "Define photosynthesis",
    ],
    "analysis": [
        "Compare microservices vs monolith", "Analyze the pros and cons",
        "Evaluate this strategy", "What are the trade-offs?",
    ],
}


class SemanticRouter:
    def __init__(self):
        self._centroids: dict[str, np.ndarray] = {}

    async def fit(self):
        """Embed all exemplars and compute per-intent centroids."""
        for intent, examples in EXEMPLARS.items():
            embeddings = [await embed(ex) for ex in examples]
            self._centroids[intent] = np.mean(embeddings, axis=0)
        print("SemanticRouter fitted.")

    async def route(self, message: str) -> tuple[str, float]:
        """Return (intent, similarity_score)."""
        msg_emb = await embed(message)
        scores = {
            intent: cosine(msg_emb, centroid)
            for intent, centroid in self._centroids.items()
        }
        best_intent = max(scores, key=scores.__getitem__)
        return best_intent, scores[best_intent]


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    router = SemanticRouter()
    await router.fit()

    tests = [
        "Fix my async Python function",
        "What's the largest country by area?",
        "Write a haiku about Kubernetes",
    ]
    for msg in tests:
        intent, score = await router.route(msg)
        route = ROUTES.get(intent, ROUTES["default"])
        print(f"[{intent} {score:.2f}] {route.model} — {msg}")


asyncio.run(main())
```

## Solution 4: Multi-signal ensemble router

Combine rule-based, length-based, and classifier signals. Weight each signal and use a majority vote to produce the final intent.

```python
import asyncio
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class Signal:
    intent: str
    weight: float
    source: str


def length_signal(message: str) -> Signal:
    """Short messages → factual; long → analysis or summarize."""
    words = len(message.split())
    if words < 15:
        return Signal("factual", 0.6, "length")
    elif words > 500:
        return Signal("summarize", 0.7, "length")
    return Signal("default", 0.2, "length")


def code_signal(message: str) -> Signal:
    """Detect code blocks or technical keywords."""
    if "```" in message or re.search(r"\b(def |class |import |async def |\.py\b)", message):
        return Signal("code", 0.9, "code_detection")
    return Signal("default", 0.1, "code_detection")


def question_type_signal(message: str) -> Signal:
    """Classify by question word."""
    msg = message.strip().lower()
    if msg.startswith(("what is ", "who is ", "when did ", "where is ", "how many ")):
        return Signal("factual", 0.75, "question_type")
    if msg.startswith(("write ", "create ", "compose ", "generate ")):
        return Signal("creative", 0.8, "question_type")
    if msg.startswith(("analyze ", "compare ", "evaluate ", "assess ")):
        return Signal("analysis", 0.8, "question_type")
    return Signal("default", 0.2, "question_type")


async def classifier_signal(message: str) -> Signal:
    """LLM-based classification (async)."""
    # Simplified — uses keyword approach here for demo
    if re.search(r"\b(debug|traceback|error|fix|bug)\b", message, re.I):
        return Signal("code", 0.85, "llm_classifier")
    return Signal("default", 0.3, "llm_classifier")


async def ensemble_route(message: str) -> tuple[str, float]:
    """Aggregate all signals into a single intent decision."""
    signals = [
        length_signal(message),
        code_signal(message),
        question_type_signal(message),
        await classifier_signal(message),
    ]

    # Aggregate weighted votes per intent
    scores: dict[str, float] = {}
    for signal in signals:
        scores[signal.intent] = scores.get(signal.intent, 0.0) + signal.weight

    # Normalize
    total = sum(scores.values())
    scores = {k: v / total for k, v in scores.items()}

    best = max(scores, key=scores.__getitem__)
    return best, scores[best]


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    messages = [
        "def fibonacci(n): # fix this function",
        "What is photosynthesis?",
        "Write a haiku about clouds",
        "Analyze the trade-offs between SQL and NoSQL databases",
    ]
    for msg in messages:
        intent, score = await ensemble_route(msg)
        route = ROUTES.get(intent, ROUTES["default"])
        print(f"[{intent} {score:.0%}] → {route.model}: {msg[:50]}")


asyncio.run(main())
```

## Solution 5: Conversation-aware router with intent persistence

Maintain intent across a multi-turn conversation. Once a user establishes "code review" mode, keep routing to the code specialist until the intent clearly shifts.

```python
import asyncio
from dataclasses import dataclass, field
from collections import deque
from anthropic import AsyncAnthropic
import json

client = AsyncAnthropic()


@dataclass
class ConversationContext:
    session_id: str
    intent_history: deque = field(default_factory=lambda: deque(maxlen=5))
    current_intent: str = "default"
    turns_since_shift: int = 0

    def update_intent(self, new_intent: str):
        self.intent_history.append(new_intent)
        if new_intent != self.current_intent:
            self.current_intent = new_intent
            self.turns_since_shift = 0
        else:
            self.turns_since_shift += 1

    def dominant_intent(self) -> str:
        """Return the most frequent recent intent."""
        if not self.intent_history:
            return "default"
        from collections import Counter
        counts = Counter(self.intent_history)
        return counts.most_common(1)[0][0]


INTENT_SHIFT_THRESHOLD = 2  # require 2+ turns before switching intent


class ConversationRouter:
    def __init__(self):
        self._sessions: dict[str, ConversationContext] = {}

    def get_context(self, session_id: str) -> ConversationContext:
        return self._sessions.setdefault(session_id, ConversationContext(session_id))

    async def route(self, session_id: str, message: str) -> RouteConfig:
        ctx = self.get_context(session_id)

        # Classify current turn
        new_intent = await self._classify(message)
        ctx.update_intent(new_intent)

        # Use dominant intent for stability — avoid thrashing
        dominant = ctx.dominant_intent()
        route = ROUTES.get(dominant, ROUTES["default"])

        print(
            f"[session={session_id}] turn_intent={new_intent} "
            f"dominant={dominant} model={route.model}"
        )
        return route

    async def _classify(self, message: str) -> str:
        # Simplified rule-based for demo
        import re
        if re.search(r"\b(code|debug|fix|implement|function)\b", message, re.I):
            return "code"
        if re.search(r"\b(summarize|tldr|brief)\b", message, re.I):
            return "summarize"
        if re.search(r"\b(analyze|compare|evaluate)\b", message, re.I):
            return "analysis"
        return "default"


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    router = ConversationRouter()
    session = "user-42"

    conversation = [
        "Fix this Python bug: def add(a, b) return a+b",
        "Now add type hints to it",
        "What time is it in Tokyo?",     # intent shift — still routes code for stability
        "Analyze the performance of this function",
    ]

    for turn in conversation:
        route = await router.route(session, turn)
        print(f"  → {route.intent} / {route.model}\n")


asyncio.run(main())
```

## Solution 6: Cost-aware adaptive router with feedback loop

Track actual cost per route (input tokens × model price). Use this to calibrate the routing thresholds — tighten routing to cheaper models as budget pressure increases.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Prices per million tokens (approximate, 2025)
MODEL_PRICES = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}


@dataclass
class CostTracker:
    budget_usd: float
    spent_usd: float = 0.0
    call_log: list[dict] = field(default_factory=list)

    @property
    def remaining_fraction(self) -> float:
        return max(0.0, 1.0 - self.spent_usd / self.budget_usd)

    def record(self, model: str, input_tokens: int, output_tokens: int):
        prices = MODEL_PRICES.get(model, MODEL_PRICES["claude-sonnet-4-6"])
        cost = (input_tokens / 1_000_000) * prices["input"] + \
               (output_tokens / 1_000_000) * prices["output"]
        self.spent_usd += cost
        self.call_log.append({"model": model, "cost_usd": round(cost, 6)})

    def budget_tier(self) -> str:
        """Return routing tier based on remaining budget."""
        f = self.remaining_fraction
        if f > 0.5:
            return "normal"    # use configured routes as-is
        elif f > 0.2:
            return "economy"   # downgrade one tier
        else:
            return "minimal"   # use haiku for everything except analysis


class CostAwareRouter:
    def __init__(self, budget_usd: float = 10.0):
        self.tracker = CostTracker(budget_usd=budget_usd)

    async def call(self, message: str, base_route: RouteConfig) -> str:
        tier = self.tracker.budget_tier()
        model = base_route.model

        # Downgrade model under budget pressure
        if tier == "economy":
            model = {
                "claude-opus-4-6": "claude-sonnet-4-6",
                "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
                "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
            }.get(model, "claude-haiku-4-5-20251001")
        elif tier == "minimal":
            model = "claude-haiku-4-5-20251001"

        max_tokens = base_route.max_tokens
        if tier != "normal":
            max_tokens = min(max_tokens, 1024)  # also cap output tokens

        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=base_route.temperature,
            system=base_route.system_prompt,
            messages=[{"role": "user", "content": message}],
        )

        usage = response.usage
        self.tracker.record(model, usage.input_tokens, usage.output_tokens)

        print(
            f"[{tier}] model={model} "
            f"tokens={usage.input_tokens}+{usage.output_tokens} "
            f"spent=${self.tracker.spent_usd:.4f}/{self.tracker.budget_usd}"
        )
        return response.content[0].text


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    router = CostAwareRouter(budget_usd=0.01)  # tiny budget for demo
    route = ROUTES["factual"]

    for i in range(3):
        result = await router.call(f"What is {i+1}+{i+1}?", route)
        print(result[:50])


asyncio.run(main())
```

## Comparison

| Approach | Latency overhead | Accuracy | Cost | Handles context | Adaptive |
|---|---|---|---|---|---|
| Rule-based keyword router | <1 ms | Medium | Zero | No | No |
| LLM classifier (haiku) | 50–200 ms | High | ~$0.001/call | No | No |
| Semantic embedding router | 5–50 ms | High | Embedding cost | No | No |
| Multi-signal ensemble | 50–200 ms | Very high | Low | No | No |
| Conversation-aware router | 50–200 ms | High | Low | Yes | No |
| Cost-aware adaptive router | 50–200 ms | High | Self-limiting | No | Yes |

**Recommendation**: Start with **rule-based routing** (Solution 1) for zero-cost classification of obvious intents. Add **LLM classifier** (Solution 2) for edge cases where rules fail. Use **conversation-aware routing** (Solution 5) for multi-turn sessions and **cost-aware routing** (Solution 6) if you operate under a hard budget ceiling.
