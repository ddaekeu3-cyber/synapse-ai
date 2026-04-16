---
title: "Agent Doesn't Implement Response-Size-Based Model Selection"
slug: agent-doesnt-implement-response-size-based-model-selection
category: performance
tags: [model-selection, routing, token-cost, output-length, performance, anthropic-sdk]
description: >
  The agent uses the same model for every request regardless of how large the
  expected output is. Short factual lookups waste money on Opus; multi-thousand-token
  synthesis tasks underperform on Haiku. Routing requests by predicted response
  size cuts costs for short outputs and improves quality for long ones.
symptoms:
  - Haiku truncates long reports that Sonnet would have completed correctly
  - Opus is used for one-word answers, inflating cost by 20x
  - No correlation between model tier and actual output length in usage logs
  - max_tokens is set to a single global constant for all request types
related_solutions:
  - agent-doesnt-implement-model-tiering-by-task-complexity
  - agent-doesnt-implement-prompt-token-budget-enforcement-per-request
  - agent-doesnt-implement-output-length-control
---

## Problem

Output length is a strong proxy for task complexity: factual Q&A rarely
exceeds 200 tokens; detailed code generation or long-form writing routinely
runs to 2 000–8 000. Using a small model for large outputs risks truncation
and quality degradation; using a large model for tiny outputs burns money.
Predicting output size before generation — from keywords, prompt structure,
or a cheap classifier call — lets you pick the right model and right
`max_tokens` value before spending any generation tokens.

---

## Solution 1 — Keyword-Based Output Size Classifier

Map request keywords to expected output-size buckets (tiny / short / medium /
large) and route each bucket to an appropriate model + max_tokens pair.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass


@dataclass
class SizeBucket:
    name:       str
    max_tokens: int
    model:      str


BUCKETS = {
    "tiny":   SizeBucket("tiny",   64,   "claude-haiku-4-5-20251001"),
    "short":  SizeBucket("short",  256,  "claude-haiku-4-5-20251001"),
    "medium": SizeBucket("medium", 1024, "claude-sonnet-4-6"),
    "large":  SizeBucket("large",  4096, "claude-sonnet-4-6"),
    "xlarge": SizeBucket("xlarge", 8192, "claude-opus-4-6"),
}

KEYWORD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(yes or no|true or false|one word|single word)\b", re.I), "tiny"),
    (re.compile(r"\b(define|what is|who is|when was|how many)\b", re.I),     "short"),
    (re.compile(r"\b(explain|describe|summarize|compare|how does)\b", re.I), "medium"),
    (re.compile(r"\b(write a|generate|implement|create a|draft)\b", re.I),   "large"),
    (re.compile(r"\b(detailed report|comprehensive|full implementation|step by step guide)\b", re.I), "xlarge"),
]


def classify_output_size(prompt: str) -> SizeBucket:
    for pattern, bucket_name in KEYWORD_RULES:
        if pattern.search(prompt):
            return BUCKETS[bucket_name]
    return BUCKETS["medium"]   # default


async def size_routed_create(
    messages: list,
    force_bucket: str | None = None,
) -> tuple[str, SizeBucket]:
    prompt_text = " ".join(
        m["content"] for m in messages if isinstance(m.get("content"), str)
    )
    bucket = BUCKETS[force_bucket] if force_bucket else classify_output_size(prompt_text)
    print(f"[size-router] bucket={bucket.name}  model={bucket.model}  max_tokens={bucket.max_tokens}")

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=bucket.model,
        max_tokens=bucket.max_tokens,
        messages=messages,
    )
    return resp.content[0].text, bucket


async def demo():
    queries = [
        "Is Python dynamically typed? Answer yes or no.",
        "What is a hash table?",
        "Explain how consistent hashing works.",
        "Write a Python implementation of a thread-safe LRU cache.",
    ]
    for q in queries:
        text, bucket = await size_routed_create([{"role": "user", "content": q}])
        print(f"[{bucket.name:6s}] {q[:45]:45s} -> {len(text):5d} chars")


asyncio.run(demo())
```

---

## Solution 2 — Token-Count Pre-Estimation with Scaling

Count input tokens using `client.messages.count_tokens()`, apply an
input-to-output ratio heuristic by task type, and derive `max_tokens` and
model dynamically before generation.

```python
import anthropic
import asyncio
import re


OUTPUT_RATIOS = {
    "qa":       0.5,   # short factual answers
    "summary":  0.3,   # summaries are shorter than source
    "explain":  1.5,   # explanation ≈ 1.5x the question length
    "code":     3.0,   # code generation often longer than prompt
    "essay":    4.0,   # long-form writing
}

MODEL_THRESHOLDS = [
    (256,  "claude-haiku-4-5-20251001"),
    (2048, "claude-sonnet-4-6"),
    (8192, "claude-opus-4-6"),
]


def infer_task_type(prompt: str) -> str:
    p = prompt.lower()
    if re.search(r"\b(code|implement|function|class|script)\b", p):
        return "code"
    if re.search(r"\b(essay|article|report|write about)\b", p):
        return "essay"
    if re.search(r"\b(summarize|tldr|brief)\b", p):
        return "summary"
    if re.search(r"\b(explain|describe|how does)\b", p):
        return "explain"
    return "qa"


def select_model_for_tokens(predicted_tokens: int) -> tuple[str, int]:
    for cap, model in MODEL_THRESHOLDS:
        if predicted_tokens <= cap:
            return model, cap
    return MODEL_THRESHOLDS[-1][1], MODEL_THRESHOLDS[-1][0]


async def token_aware_create(messages: list) -> str:
    client = anthropic.AsyncAnthropic()

    # Count input tokens
    count_resp = await client.messages.count_tokens(
        model="claude-sonnet-4-6",
        messages=messages,
    )
    input_tokens = count_resp.input_tokens

    prompt_text = " ".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))
    task_type = infer_task_type(prompt_text)
    ratio = OUTPUT_RATIOS[task_type]
    predicted_output = max(64, int(input_tokens * ratio))

    model, max_tokens = select_model_for_tokens(predicted_output)
    print(
        f"[token-aware] input={input_tokens}  task={task_type}  "
        f"predicted_output={predicted_output}  model={model}  max_tokens={max_tokens}"
    )

    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    actual = resp.usage.output_tokens
    print(f"[token-aware] actual_output={actual}  prediction_error={abs(actual-predicted_output)}")
    return resp.content[0].text


async def demo():
    cases = [
        "What year was Python created?",
        "Explain gradient descent in machine learning.",
        "Write a Python class implementing a min-heap with insert and extract_min.",
    ]
    for q in cases:
        text = await token_aware_create([{"role": "user", "content": q}])
        print(f"Response ({len(text)} chars): {text[:60]}\n")


asyncio.run(demo())
```

---

## Solution 3 — Lightweight Pre-Classifier Using Haiku

Use a cheap Haiku call to predict the output size bucket before the main
generation. The classifier cost is negligible (< 50 tokens) compared to
savings from routing a large request to the right model.

```python
import anthropic
import asyncio
import json
import re


CLASSIFIER_PROMPT = """\
Predict the output length of answering the following prompt.
Respond with JSON only: {"bucket": "<tiny|short|medium|large|xlarge>"}
- tiny:   < 50 tokens (yes/no, single word)
- short:  50–200 tokens (definition, brief fact)
- medium: 200–800 tokens (explanation, comparison)
- large:  800–3000 tokens (implementation, detailed analysis)
- xlarge: > 3000 tokens (comprehensive report, full codebase)

Prompt to classify:
"""

MODEL_FOR_BUCKET = {
    "tiny":   ("claude-haiku-4-5-20251001",  64),
    "short":  ("claude-haiku-4-5-20251001",  256),
    "medium": ("claude-sonnet-4-6",           1024),
    "large":  ("claude-sonnet-4-6",           4096),
    "xlarge": ("claude-opus-4-6",             8192),
}


async def classify_with_haiku(prompt: str) -> str:
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": CLASSIFIER_PROMPT + prompt}],
    )
    raw = resp.content[0].text
    m = re.search(r'"bucket"\s*:\s*"(\w+)"', raw)
    if m and m.group(1) in MODEL_FOR_BUCKET:
        return m.group(1)
    return "medium"


async def pre_classified_create(messages: list) -> str:
    prompt = " ".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))

    # Run classifier and main request; classifier is fast
    bucket = await classify_with_haiku(prompt)
    model, max_tokens = MODEL_FOR_BUCKET[bucket]
    print(f"[pre-classifier] bucket={bucket}  model={model}  max_tokens={max_tokens}")

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    return resp.content[0].text


async def demo():
    cases = [
        "Is Java object-oriented? Answer with one word.",
        "What is the observer design pattern?",
        "Write a complete REST API in FastAPI with JWT authentication, CRUD endpoints for users, and OpenAPI docs.",
    ]
    for q in cases:
        text = await pre_classified_create([{"role": "user", "content": q}])
        print(f"[{len(text):5d} chars] {q[:50]}\n")


asyncio.run(demo())
```

---

## Solution 4 — Adaptive max_tokens from Streaming Stop Reason

On the first attempt, use a conservative `max_tokens`. If `stop_reason ==
"max_tokens"` (truncated), automatically retry with a larger value and a
higher-tier model without requiring user intervention.

```python
import anthropic
import asyncio


TIERS = [
    ("claude-haiku-4-5-20251001",  512),
    ("claude-haiku-4-5-20251001",  2048),
    ("claude-sonnet-4-6",          4096),
    ("claude-sonnet-4-6",          8096),
    ("claude-opus-4-6",            8096),
]


async def adaptive_max_tokens_create(
    messages: list,
    start_tier: int = 0,
) -> tuple[str, str, int]:
    """Returns (text, model, max_tokens_used)."""
    client = anthropic.AsyncAnthropic()

    for tier_idx in range(start_tier, len(TIERS)):
        model, max_tokens = TIERS[tier_idx]
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        text = resp.content[0].text
        actual = resp.usage.output_tokens
        print(
            f"[adaptive] tier={tier_idx}  model={model}  "
            f"max={max_tokens}  actual={actual}  stop={resp.stop_reason}"
        )

        if resp.stop_reason != "max_tokens":
            return text, model, max_tokens

        # Truncated — escalate to next tier
        if tier_idx + 1 < len(TIERS):
            print(f"[adaptive] truncated at {actual} tokens — escalating to tier {tier_idx + 1}")

    # Last tier was still truncated — return what we have
    return text, model, max_tokens


async def demo():
    # Short response — should stop at tier 0
    short_text, m, mt = await adaptive_max_tokens_create(
        [{"role": "user", "content": "What does API stand for?"}]
    )
    print(f"Short: model={m}  tokens_budget={mt}  len={len(short_text)}\n")

    # Longer response
    long_text, m, mt = await adaptive_max_tokens_create(
        [{"role": "user", "content": "Write a comprehensive comparison of REST vs GraphQL vs gRPC."}],
        start_tier=1,
    )
    print(f"Long:  model={m}  tokens_budget={mt}  len={len(long_text)}\n")


asyncio.run(demo())
```

---

## Solution 5 — Historical Output Length Tracker with Learned Routing

Record actual output token counts per prompt-type fingerprint and use the
historical p90 to set `max_tokens` and choose the model for future requests
of the same type. The router learns from real traffic.

```python
import anthropic
import asyncio
import hashlib
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field


MODEL_CAPS = [
    (256,   "claude-haiku-4-5-20251001"),
    (2048,  "claude-sonnet-4-6"),
    (8192,  "claude-opus-4-6"),
]

SAFETY_MULT = 1.4   # headroom over p90


@dataclass
class HistoryBucket:
    samples: deque = field(default_factory=lambda: deque(maxlen=100))

    def record(self, tokens: int) -> None:
        self.samples.append(tokens)

    def p90(self) -> int | None:
        if len(self.samples) < 3:
            return None
        s = sorted(self.samples)
        idx = math.ceil(0.90 * len(s)) - 1
        return s[max(0, idx)]


_history: dict[str, HistoryBucket] = defaultdict(HistoryBucket)


def _prompt_type_key(prompt: str) -> str:
    # Normalize to a type fingerprint: first 5 significant words
    words = re.findall(r'\b[a-z]{3,}\b', prompt.lower())[:5]
    return hashlib.md5(" ".join(words).encode()).hexdigest()[:8]


def _select_tier(predicted: int) -> tuple[str, int]:
    capped = int(predicted * SAFETY_MULT)
    for cap, model in MODEL_CAPS:
        if capped <= cap:
            return model, cap
    return MODEL_CAPS[-1][1], MODEL_CAPS[-1][0]


async def history_routed_create(messages: list) -> str:
    prompt = " ".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))
    pkey = _prompt_type_key(prompt)
    bucket = _history[pkey]
    p90 = bucket.p90()

    if p90:
        model, max_tokens = _select_tier(p90)
        print(f"[history-router] key={pkey}  p90={p90}  model={model}  max_tokens={max_tokens}")
    else:
        # Cold start: use medium defaults
        model, max_tokens = "claude-sonnet-4-6", 1024
        print(f"[history-router] key={pkey}  cold-start  model={model}  max_tokens={max_tokens}")

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, messages=messages
    )
    actual = resp.usage.output_tokens
    bucket.record(actual)
    print(f"[history-router] actual={actual}  samples={len(bucket.samples)}")
    return resp.content[0].text


async def demo():
    # Warm up with 3 calls of same type
    for i in range(3):
        await history_routed_create([{"role": "user", "content": "What is a binary search tree?"}])

    # 4th call uses learned p90
    result = await history_routed_create([{"role": "user", "content": "What is a red-black tree?"}])
    print(f"Learned-route result: {result[:80]}")


asyncio.run(demo())
```

---

## Solution 6 — Parallel Probe-and-Select (Speculative Sizing)

Fire a streaming haiku call with a low `max_tokens`. If it completes without
truncation, use that result (fast + cheap). If it hits the limit, cancel and
re-run with a larger model at the right capacity. Median response time is
haiku-fast; only outliers pay the escalation cost.

```python
import anthropic
import asyncio


PROBE_MAX_TOKENS    = 384
PROBE_MODEL         = "claude-haiku-4-5-20251001"
FALLBACK_MODEL      = "claude-sonnet-4-6"
FALLBACK_MAX_TOKENS = 4096


async def speculative_create(messages: list) -> tuple[str, str]:
    """Returns (text, model_used)."""
    client = anthropic.AsyncAnthropic()

    # Probe attempt
    probe_resp = await client.messages.create(
        model=PROBE_MODEL,
        max_tokens=PROBE_MAX_TOKENS,
        messages=messages,
    )

    if probe_resp.stop_reason != "max_tokens":
        print(
            f"[speculative] probe COMPLETE  "
            f"model={PROBE_MODEL}  tokens={probe_resp.usage.output_tokens}"
        )
        return probe_resp.content[0].text, PROBE_MODEL

    # Probe was truncated — escalate
    print(
        f"[speculative] probe TRUNCATED at {probe_resp.usage.output_tokens} tokens "
        f"— escalating to {FALLBACK_MODEL}"
    )
    full_resp = await client.messages.create(
        model=FALLBACK_MODEL,
        max_tokens=FALLBACK_MAX_TOKENS,
        messages=messages,
    )
    print(
        f"[speculative] fallback  "
        f"model={FALLBACK_MODEL}  tokens={full_resp.usage.output_tokens}"
    )
    return full_resp.content[0].text, FALLBACK_MODEL


async def demo():
    cases = [
        "What does REST stand for?",
        "Write a complete Python implementation of Dijkstra's shortest-path algorithm with test cases.",
    ]
    for q in cases:
        text, model = await speculative_create([{"role": "user", "content": q}])
        print(f"[{model}] ({len(text)} chars) {text[:60]}\n")


asyncio.run(demo())
```

---

## Comparison

| Approach | Prediction method | Latency overhead | Learning | Truncation risk | Complexity |
|---|---|---|---|---|---|
| Keyword-based classifier | Regex rules | Zero | No | Low (conservative caps) | Very low |
| Token-count pre-estimation | Count API + ratio | 1 extra API call | No | Low | Low |
| Haiku pre-classifier | LLM classification | 1 cheap API call | No | Very low | Low |
| Adaptive stop-reason escalation | Post-hoc detection | Retry cost on truncation | No | None (auto-retry) | Medium |
| Historical p90 tracker | Real traffic p90 | Zero (learned) | Yes | Low | Medium |
| Speculative probe-and-select | Optimistic fast path | Probe cost on short wins | No | None | Medium |

**Rule of thumb:**
- New system, no traffic data → keyword rules (Solution 1) to bootstrap, then layer historical tracker (Solution 5)
- Mostly short responses with occasional long ones → speculative probe (Solution 6) for best median latency
- Structured prompts with predictable types → Haiku pre-classifier (Solution 3)
- Never allow truncation → adaptive escalation (Solution 4) as a safety net on top of any other approach
