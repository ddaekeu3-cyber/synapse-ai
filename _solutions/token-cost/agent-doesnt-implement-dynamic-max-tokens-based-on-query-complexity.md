---
layout: solution
title: "Agent Doesn't Implement Dynamic Max Tokens Based on Query Complexity"
category: token-cost
description: "Agents that set a fixed max_tokens ceiling waste budget on simple queries and truncate complex ones. These patterns show how to calibrate max_tokens dynamically — from lightweight heuristics to LLM-scored complexity routing."
tags: [token-cost, max-tokens, complexity, routing, optimization, anthropic]
---

## Problem

A fixed `max_tokens=4096` on every call is wasteful: a yes/no factual question doesn't need four thousand tokens, while a detailed architecture explanation might need eight thousand. Static ceilings either burn budget or silently truncate long responses. The fix is dynamic calibration — measure query complexity and set an appropriate ceiling before each call.

---

### Option 1: Heuristic Length and Keyword Scoring

Score complexity from query word count and presence of complexity keywords, then map to a token tier.

```python
import re
import anthropic

client = anthropic.Anthropic()

COMPLEXITY_KEYWORDS = {
    "high": ["explain", "compare", "analyze", "design", "architecture", "implement",
             "step-by-step", "tradeoffs", "pros and cons", "how does", "difference between"],
    "medium": ["what is", "why does", "when should", "list", "summarize", "overview"],
    "low": ["yes or no", "true or false", "which", "who", "when", "where", "define"],
}

TOKEN_TIERS = {
    "low": 256,
    "medium": 1024,
    "high": 4096,
}

def score_complexity(query: str) -> str:
    q = query.lower()
    word_count = len(q.split())

    for level, keywords in COMPLEXITY_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            # word count can bump it up
            if level == "low" and word_count > 30:
                return "medium"
            return level

    if word_count < 10:
        return "low"
    elif word_count < 40:
        return "medium"
    else:
        return "high"

def dynamic_call(query: str) -> str:
    tier = score_complexity(query)
    max_tokens = TOKEN_TIERS[tier]
    print(f"[complexity={tier}, max_tokens={max_tokens}]")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

if __name__ == "__main__":
    queries = [
        "Is Python interpreted?",
        "What is a REST API?",
        "Compare microservices vs monolith architecture with tradeoffs.",
    ]
    for q in queries:
        print(f"\nQ: {q}")
        print(dynamic_call(q))

# Expected Token Savings: 60-80% reduction for simple queries vs fixed 4096 ceiling
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Structural Feature Extraction

Parse structural signals — question marks, conjunctions, code blocks, list requests — to build a complexity score.

```python
import re
import anthropic

client = anthropic.Anthropic()

def extract_features(query: str) -> dict:
    q = query.lower()
    return {
        "word_count": len(query.split()),
        "question_count": query.count("?"),
        "has_code_request": bool(re.search(r"\bcode\b|\bimplement\b|\bwrite\b|\bexample\b", q)),
        "has_list_request": bool(re.search(r"\blist\b|\bsteps\b|\bexamples\b|\ball\b", q)),
        "conjunction_count": len(re.findall(r"\band\b|\bbut\b|\bhowever\b|\bwhereas\b|\bmoreover\b", q)),
        "has_comparison": bool(re.search(r"\bvs\b|\bversus\b|\bcompare\b|\bdifference\b", q)),
    }

def features_to_max_tokens(f: dict) -> int:
    score = 0
    score += min(f["word_count"] // 10, 5)         # 0-5
    score += f["question_count"] * 2                 # 0+
    score += 4 if f["has_code_request"] else 0
    score += 3 if f["has_list_request"] else 0
    score += f["conjunction_count"]
    score += 5 if f["has_comparison"] else 0

    if score <= 3:
        return 256
    elif score <= 7:
        return 512
    elif score <= 12:
        return 2048
    else:
        return 6000

def call_with_features(query: str) -> str:
    features = extract_features(query)
    max_tokens = features_to_max_tokens(features)
    print(f"[features={features}, max_tokens={max_tokens}]")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

if __name__ == "__main__":
    test_queries = [
        "Is Redis persistent?",
        "List 10 Python best practices with examples.",
        "Compare and contrast SQL vs NoSQL databases, including schema, scaling, consistency, and when to use each.",
    ]
    for q in test_queries:
        print(f"\nQ: {q}")
        print(call_with_features(q))

# Expected Token Savings: 50-75% for simple queries; avoids truncation on complex multi-part questions
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Fast Classifier Pre-flight with Haiku

Use a cheap Haiku call to classify complexity before the main call — spend ~50 tokens to save thousands.

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_TOKENS_MAP = {
    "trivial": 128,
    "simple": 512,
    "moderate": 2048,
    "complex": 6000,
    "exhaustive": 8192,
}

CLASSIFIER_PROMPT = """Classify the complexity of this user query into exactly one of:
trivial, simple, moderate, complex, exhaustive

trivial: yes/no, single fact, under 5 words answer
simple: 1-3 sentences answer, single concept
moderate: paragraph or two, some explanation needed
complex: multi-part, detailed explanation, code examples
exhaustive: comprehensive guide, architecture, many sections

Respond with JSON: {"complexity": "<level>", "reason": "<one sentence>"}

Query: {query}"""

def classify_complexity(query: str) -> tuple[str, int]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": CLASSIFIER_PROMPT.format(query=query),
        }],
    )
    raw = response.content[0].text.strip()
    try:
        data = json.loads(raw)
        level = data.get("complexity", "moderate")
        reason = data.get("reason", "")
    except json.JSONDecodeError:
        level, reason = "moderate", "parse error"

    max_tok = MAX_TOKENS_MAP.get(level, 2048)
    return level, max_tok, reason

def smart_call(query: str, model: str = "claude-sonnet-4-6") -> str:
    level, max_tokens, reason = classify_complexity(query)
    print(f"[complexity={level}, max_tokens={max_tokens}] {reason}")

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

if __name__ == "__main__":
    queries = [
        "What's 2+2?",
        "What is dependency injection?",
        "Write a complete async web scraper in Python with rate limiting and retry logic.",
    ]
    for q in queries:
        print(f"\nQ: {q}")
        print(smart_call(q))

# Expected Token Savings: 70-85% on trivial/simple; classifier costs ~50 tokens, saves thousands
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Response-Length Estimation via Streaming Token Count

Stream a short sample and extrapolate response length to cap subsequent retries or confirm the ceiling was appropriate.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

INITIAL_CEILINGS = {
    "short": 512,
    "medium": 2048,
    "long": 8192,
}

def initial_ceiling(query: str) -> int:
    words = len(query.split())
    if words < 15:
        return INITIAL_CEILINGS["short"]
    elif words < 50:
        return INITIAL_CEILINGS["medium"]
    return INITIAL_CEILINGS["long"]

async def stream_with_adaptive_ceiling(query: str) -> str:
    ceiling = initial_ceiling(query)
    print(f"[initial ceiling={ceiling}]")

    chunks = []
    stop_reason = None

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=ceiling,
        messages=[{"role": "user", "content": query}],
    ) as stream:
        async for text in stream.text_stream:
            chunks.append(text)
        final = await stream.get_final_message()
        stop_reason = final.stop_reason

    result = "".join(chunks)
    token_count = final.usage.output_tokens

    print(f"[stop_reason={stop_reason}, output_tokens={token_count}]")

    # If we hit the ceiling, retry with a higher one
    if stop_reason == "max_tokens" and ceiling < 8192:
        new_ceiling = min(ceiling * 2, 8192)
        print(f"[hit ceiling, retrying with max_tokens={new_ceiling}]")
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=new_ceiling,
            messages=[{"role": "user", "content": query}],
        ) as stream2:
            chunks2 = []
            async for text in stream2.text_stream:
                chunks2.append(text)
            result = "".join(chunks2)

    return result

if __name__ == "__main__":
    test = "Explain the CAP theorem with detailed examples for each combination."
    result = asyncio.run(stream_with_adaptive_ceiling(test))
    print(result[:500])

# Expected Token Savings: Avoids pre-allocating 8192 when 512 suffices; retry overhead only when needed
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Multi-Signal Ensemble with Confidence Weighting

Combine keyword scoring, length features, and a classifier into a weighted ensemble for robust ceiling selection.

```python
import re
import json
import asyncio
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class ComplexitySignal:
    name: str
    score: float      # 0.0 (trivial) to 1.0 (exhaustive)
    weight: float
    confidence: float

def keyword_signal(query: str) -> ComplexitySignal:
    q = query.lower()
    high_kw = ["implement", "design", "architecture", "compare", "analyze", "comprehensive"]
    low_kw = ["what is", "define", "who", "when", "yes or no"]
    high_hits = sum(1 for kw in high_kw if kw in q)
    low_hits = sum(1 for kw in low_kw if kw in q)
    score = min(1.0, high_hits * 0.25) - min(0.5, low_hits * 0.25) + 0.3
    score = max(0.0, min(1.0, score))
    return ComplexitySignal("keyword", score, weight=0.3, confidence=0.7)

def length_signal(query: str) -> ComplexitySignal:
    words = len(query.split())
    score = min(1.0, words / 60)
    return ComplexitySignal("length", score, weight=0.2, confidence=0.9)

def structure_signal(query: str) -> ComplexitySignal:
    q = query.lower()
    parts = 0
    parts += query.count("?")
    parts += len(re.findall(r"\band\b", q))
    parts += 2 if re.search(r"\bcode\b|\bexample\b", q) else 0
    score = min(1.0, parts / 8)
    return ComplexitySignal("structure", score, weight=0.2, confidence=0.8)

async def llm_signal(query: str) -> ComplexitySignal:
    prompt = f"""Rate complexity 0-10 for expected response length. 0=one word, 10=multi-page guide.
Query: {query}
JSON: {{"score": <0-10>}}"""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(resp.content[0].text.strip())
        score = float(data["score"]) / 10.0
    except Exception:
        score = 0.5
    return ComplexitySignal("llm", score, weight=0.3, confidence=0.85)

def score_to_max_tokens(score: float) -> int:
    if score < 0.2:
        return 128
    elif score < 0.4:
        return 512
    elif score < 0.6:
        return 2048
    elif score < 0.8:
        return 4096
    else:
        return 8192

async def ensemble_call(query: str) -> str:
    signals = await asyncio.gather(
        asyncio.to_thread(keyword_signal, query),
        asyncio.to_thread(length_signal, query),
        asyncio.to_thread(structure_signal, query),
        llm_signal(query),
    )

    total_weight = sum(s.weight * s.confidence for s in signals)
    weighted_score = sum(s.score * s.weight * s.confidence for s in signals) / total_weight

    max_tokens = score_to_max_tokens(weighted_score)
    print(f"[ensemble_score={weighted_score:.3f}, max_tokens={max_tokens}]")
    for s in signals:
        print(f"  [{s.name}: {s.score:.2f} w={s.weight} c={s.confidence}]")

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

if __name__ == "__main__":
    q = "Design a fault-tolerant distributed task queue with at-least-once delivery guarantees."
    result = asyncio.run(ensemble_call(q))
    print(result[:600])

# Expected Token Savings: Most accurate ceiling selection; avoids both over/under-allocation
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Session-Level Calibration with Feedback Loop

Track actual output token counts per query type and adjust future ceilings using exponential moving average.

```python
import re
import json
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class SessionCalibrator:
    alpha: float = 0.3          # EMA smoothing factor
    ema_by_type: dict = field(default_factory=lambda: defaultdict(lambda: 1024.0))
    sample_counts: dict = field(default_factory=lambda: defaultdict(int))
    padding_factor: float = 1.4  # headroom above EMA

    def classify_type(self, query: str) -> str:
        q = query.lower()
        if re.search(r"\bcode\b|\bimplement\b|\bwrite\b", q):
            return "code"
        if re.search(r"\bcompare\b|\bvs\b|\bversus\b|\btradeoff", q):
            return "comparison"
        if re.search(r"\blist\b|\bsteps\b|\ball\b", q):
            return "list"
        if re.search(r"\bwhat is\b|\bdefine\b|\bwho\b", q):
            return "factual"
        return "general"

    def get_ceiling(self, query: str) -> tuple[str, int]:
        qtype = self.classify_type(query)
        ema = self.ema_by_type[qtype]
        ceiling = int(min(ema * self.padding_factor, 8192))
        ceiling = max(ceiling, 128)
        # Round to nearest 256
        ceiling = ((ceiling + 255) // 256) * 256
        return qtype, ceiling

    def update(self, qtype: str, actual_tokens: int) -> None:
        old = self.ema_by_type[qtype]
        self.ema_by_type[qtype] = self.alpha * actual_tokens + (1 - self.alpha) * old
        self.sample_counts[qtype] += 1
        print(f"  [calibrate {qtype}: actual={actual_tokens}, new_ema={self.ema_by_type[qtype]:.0f}]")

    def summary(self) -> dict:
        return {k: {"ema": round(v), "samples": self.sample_counts[k]}
                for k, v in self.ema_by_type.items() if self.sample_counts[k] > 0}

calibrator = SessionCalibrator()

async def calibrated_call(query: str) -> str:
    qtype, ceiling = calibrator.get_ceiling(query)
    print(f"[type={qtype}, ceiling={ceiling}]")

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=ceiling,
        messages=[{"role": "user", "content": query}],
    )
    actual = response.usage.output_tokens
    calibrator.update(qtype, actual)

    return response.content[0].text

async def run_session():
    queries = [
        "What is a hash map?",
        "List all HTTP status codes with descriptions.",
        "Compare async/await vs callbacks in Node.js.",
        "Is Python dynamically typed?",
        "Write a binary search implementation in Python with tests.",
        "What is eventual consistency?",
        "List the SOLID principles with examples.",
        "Compare REST vs GraphQL APIs for a mobile app backend.",
    ]

    for q in queries:
        print(f"\nQ: {q}")
        result = await calibrated_call(q)
        print(result[:200])

    print("\n=== Calibration Summary ===")
    print(json.dumps(calibrator.summary(), indent=2))

if __name__ == "__main__":
    asyncio.run(run_session())

# Expected Token Savings: Self-tuning per query type; improves over session from 40% to 70%+ savings
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Approach | Latency Overhead | Accuracy | Best For |
|--------|----------|-----------------|----------|----------|
| 1 | Keyword + word count heuristic | None | Low-Medium | Quick drop-in, no extra calls |
| 2 | Structural feature extraction | None | Medium | More signals without extra cost |
| 3 | Haiku classifier pre-flight | +1 cheap call | High | When accuracy matters more than speed |
| 4 | Streaming + adaptive retry | None (retry if needed) | Medium | When you want to detect truncation |
| 5 | Multi-signal ensemble | +1 cheap call | Highest | Best ceiling accuracy across all query types |
| 6 | Session calibration with EMA | None (learns over time) | Improves | Long-running sessions with query patterns |
