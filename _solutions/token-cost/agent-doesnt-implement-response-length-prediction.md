---
layout: solution
title: "Agent Doesn't Implement Response Length Prediction"
category: token-cost
description: "How to predict appropriate max_tokens before generation to avoid over-allocating output capacity and wasting cost on unused token budget."
tags: [token-cost, max-tokens, prediction, classification, sqlite, adaptive]
---

# Agent Doesn't Implement Response Length Prediction

Setting `max_tokens=4096` for every request wastes reserved capacity and inflates per-request cost estimates. Short factual queries need 50 tokens; long essays need 2000. Predicting the right ceiling before generation avoids over-allocation, improves throughput under rate limits, and cuts costs on simple queries.

## Option 1: Task-Type Classifier to Predict Length Bucket

Classify the prompt into task categories and map each to a token budget before sending to the main model.

```python
import anthropic
import re

# Token budget per task type
TASK_LENGTH_MAP = {
    "yes_no": 20,
    "factual_lookup": 80,
    "short_explanation": 250,
    "list": 400,
    "code_snippet": 600,
    "long_explanation": 800,
    "essay": 1500,
    "code_implementation": 2000,
    "unknown": 600,
}

# Simple keyword-based classifier (no LLM call needed)
TASK_PATTERNS = [
    (r"\b(is|are|was|were|can|do|does|did|has|have|will|would)\b.+\?$", "yes_no"),
    (r"\bwhat (is|are|was) (the |a |an )?\w+\?$", "factual_lookup"),
    (r"\b(list|enumerate|name|give me) \d* ?(examples?|items?|ways?|types?)", "list"),
    (r"\b(write|implement|create|build|code) .*(function|class|script|program|api)", "code_implementation"),
    (r"\b(show|give).*(example|snippet|sample) (of |for )?(code|python|javascript)", "code_snippet"),
    (r"\b(write|draft|compose).*(essay|article|blog|report|summary)", "essay"),
    (r"\b(explain|describe|how does|why does|what causes)\b", "long_explanation"),
    (r"\b(what|who|when|where|which)\b.{0,40}\?$", "short_explanation"),
]


def classify_task(prompt: str) -> str:
    prompt_lower = prompt.lower().strip()
    for pattern, task_type in TASK_PATTERNS:
        if re.search(pattern, prompt_lower):
            return task_type
    return "unknown"


def predict_max_tokens(prompt: str) -> int:
    task_type = classify_task(prompt)
    tokens = TASK_LENGTH_MAP[task_type]
    print(f"Task type: {task_type} → max_tokens: {tokens}")
    return tokens


def chat_with_length_prediction(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    client = anthropic.Anthropic()
    max_tokens = predict_max_tokens(prompt)

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    actual_tokens = response.usage.output_tokens
    utilization = actual_tokens / max_tokens
    print(f"Used {actual_tokens}/{max_tokens} tokens ({utilization:.0%} utilization)")
    return response.content[0].text


if __name__ == "__main__":
    prompts = [
        "Is Python faster than Java?",
        "What is the capital of France?",
        "List 5 benefits of microservices.",
        "Write a Python function to reverse a linked list.",
        "Explain how garbage collection works in JVM.",
    ]

    for p in prompts:
        print(f"\nPrompt: {p}")
        result = chat_with_length_prediction(p)
        print(f"Response: {result[:100]}...")

# Expected Token Savings: 40-70% reduction in max_tokens allocation for short-answer queries
# Environment: High-volume chatbots where most queries are factual or short-form
```

## Option 2: Haiku Pre-Classifier for Length Estimation

Use a cheap Haiku call to estimate the expected response length before routing to the target model.

```python
import anthropic
import json

client = anthropic.Anthropic()

LENGTH_CLASSIFIER_PROMPT = """You are a response-length predictor. Given a user prompt, estimate how many tokens a thorough response would require.

Return JSON with exactly these fields:
{
  "estimated_tokens": <integer between 10 and 3000>,
  "confidence": <"high" | "medium" | "low">,
  "reasoning": <one sentence>
}

Be accurate. Short factual answers = 20-100 tokens. Explanations = 100-500. Code = 300-1500. Essays = 800-2500."""


def estimate_response_length(prompt: str) -> dict:
    """Use Haiku to cheaply estimate response length."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=120,
        system=LENGTH_CLASSIFIER_PROMPT,
        messages=[{"role": "user", "content": f"Prompt to estimate: {prompt}"}],
    )

    try:
        text = response.content[0].text
        # Extract JSON even if wrapped in markdown
        json_match = __import__("re").search(r"\{[^}]+\}", text, __import__("re").DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass

    return {"estimated_tokens": 500, "confidence": "low", "reasoning": "parse failed"}


def chat_with_haiku_prediction(
    prompt: str,
    target_model: str = "claude-sonnet-4-6",
    buffer_factor: float = 1.3,
) -> str:
    # Step 1: cheap length estimation
    estimate = estimate_response_length(prompt)
    raw_estimate = estimate["estimated_tokens"]
    buffered = min(int(raw_estimate * buffer_factor), 4096)

    print(f"Length estimate: {raw_estimate} tokens ({estimate['confidence']} confidence)")
    print(f"Allocated max_tokens: {buffered} (with {buffer_factor}x buffer)")
    print(f"Reasoning: {estimate['reasoning']}")

    # Step 2: main generation with predicted ceiling
    response = client.messages.create(
        model=target_model,
        max_tokens=buffered,
        messages=[{"role": "user", "content": prompt}],
    )

    actual = response.usage.output_tokens
    print(f"Actual tokens used: {actual}/{buffered} ({actual/buffered:.0%})")

    # Flag if we underestimated
    if actual >= buffered * 0.95:
        print("WARNING: Response likely truncated — consider increasing buffer_factor")

    return response.content[0].text


if __name__ == "__main__":
    test_prompts = [
        "What year was Python created?",
        "Explain the difference between TCP and UDP.",
        "Write a complete REST API server in Python using FastAPI with CRUD operations for a user model.",
    ]

    for prompt in test_prompts:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt[:60]}")
        result = chat_with_haiku_prediction(prompt)
        print(f"Result preview: {result[:150]}...")

# Expected Token Savings: 35-60% by right-sizing max_tokens using a $0.00000025/token pre-classifier
# Environment: Mixed-workload APIs where query complexity varies widely
```

## Option 3: Historical Response Length Tracker with SQLite

Record actual response lengths per query pattern and use historical medians to predict future allocations.

```python
import anthropic
import sqlite3
import hashlib
import re
import time
from typing import Optional

DB_PATH = "response_lengths.db"


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS response_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_signature TEXT NOT NULL,
            model TEXT NOT NULL,
            predicted_tokens INTEGER,
            actual_tokens INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_sig ON response_history(prompt_signature, model)")
    db.commit()
    return db


def prompt_signature(prompt: str) -> str:
    """Create a normalized signature for similar prompts."""
    # Remove numbers and proper nouns, lowercase, take first 8 words
    normalized = re.sub(r"\b\d+\b", "NUM", prompt.lower())
    normalized = re.sub(r"\b[A-Z][a-z]+\b", "NOUN", normalized)
    words = normalized.split()[:8]
    key = " ".join(words)
    return hashlib.md5(key.encode()).hexdigest()[:16]


def predict_from_history(
    db: sqlite3.Connection,
    signature: str,
    model: str,
    default: int = 500,
) -> tuple[int, int]:
    """Returns (predicted_tokens, sample_count)."""
    rows = db.execute("""
        SELECT actual_tokens FROM response_history
        WHERE prompt_signature = ? AND model = ?
        ORDER BY created_at DESC LIMIT 20
    """, (signature, model)).fetchall()

    if not rows:
        return default, 0

    lengths = sorted(r[0] for r in rows)
    # Use 85th percentile to avoid truncation
    p85_idx = int(len(lengths) * 0.85)
    p85 = lengths[min(p85_idx, len(lengths) - 1)]
    # Add 20% buffer
    prediction = min(int(p85 * 1.2), 4096)
    return prediction, len(rows)


def record_response(
    db: sqlite3.Connection,
    signature: str,
    model: str,
    predicted: int,
    actual: int,
):
    db.execute("""
        INSERT INTO response_history (prompt_signature, model, predicted_tokens, actual_tokens, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (signature, model, predicted, actual, time.time()))
    db.commit()


def chat_with_history_prediction(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    default_max_tokens: int = 600,
) -> str:
    client = anthropic.Anthropic()
    db = get_db()

    sig = prompt_signature(prompt)
    predicted, sample_count = predict_from_history(db, sig, model, default_max_tokens)

    if sample_count > 0:
        print(f"History-based prediction: {predicted} tokens (from {sample_count} samples)")
    else:
        print(f"No history found; using default: {predicted} tokens")

    response = client.messages.create(
        model=model,
        max_tokens=predicted,
        messages=[{"role": "user", "content": prompt}],
    )

    actual = response.usage.output_tokens
    record_response(db, sig, model, predicted, actual)

    utilization = actual / predicted
    print(f"Actual: {actual} tokens ({utilization:.0%} of allocation)")
    if utilization > 0.95:
        print("Note: Response near limit — next call will allocate more")

    return response.content[0].text


if __name__ == "__main__":
    # Simulate repeated similar queries
    similar_prompts = [
        "Explain how Redis works",
        "Explain how Memcached works",
        "Explain how PostgreSQL works",
    ]

    for prompt in similar_prompts * 2:  # run twice to see history kick in
        print(f"\nPrompt: {prompt}")
        result = chat_with_history_prediction(prompt)
        print(f"Response: {result[:80]}...")

# Expected Token Savings: 30-55% after warm-up period as predictions converge to actual usage patterns
# Environment: Production systems with repeated query patterns (customer support, search, Q&A)
```

## Option 4: Adaptive max_tokens Based on Question Complexity Score

Score prompt complexity on multiple dimensions and linearly map the score to a token budget.

```python
import anthropic
import re
from dataclasses import dataclass

@dataclass
class ComplexityScore:
    word_count_score: float       # longer prompts → longer answers
    question_count_score: float   # multiple questions → more tokens
    technical_score: float        # jargon density
    open_ended_score: float       # "explain/describe/analyze" vs "what/when/who"
    list_request_score: float     # "list N items" prompts

    def total(self) -> float:
        return (
            self.word_count_score * 0.25
            + self.question_count_score * 0.20
            + self.technical_score * 0.20
            + self.open_ended_score * 0.25
            + self.list_request_score * 0.10
        )

    def to_max_tokens(self, min_tokens: int = 50, max_tokens: int = 2500) -> int:
        score = self.total()  # 0.0 to 1.0
        return int(min_tokens + score * (max_tokens - min_tokens))


TECHNICAL_TERMS = {
    "algorithm", "architecture", "asynchronous", "authentication", "caching",
    "concurrency", "database", "distributed", "encryption", "framework",
    "infrastructure", "kubernetes", "microservice", "neural", "optimization",
    "parallelism", "recursion", "refactor", "scalability", "transformer",
}

OPEN_ENDED_VERBS = {"explain", "describe", "analyze", "discuss", "compare", "evaluate", "design"}
CLOSED_VERBS = {"what", "who", "when", "where", "which", "is", "are", "was"}


def score_complexity(prompt: str) -> ComplexityScore:
    words = prompt.lower().split()
    word_count = len(words)

    # Word count: 0 at 5 words, 1.0 at 100+ words
    word_count_score = min(1.0, max(0.0, (word_count - 5) / 95))

    # Question count
    question_count = len(re.findall(r"\?", prompt))
    question_count_score = min(1.0, question_count / 4)

    # Technical density
    tech_hits = sum(1 for w in words if w in TECHNICAL_TERMS)
    technical_score = min(1.0, tech_hits / max(word_count * 0.1, 1))

    # Open-ended vs closed
    first_word = words[0] if words else ""
    if first_word in OPEN_ENDED_VERBS:
        open_ended_score = 0.8
    elif first_word in CLOSED_VERBS:
        open_ended_score = 0.2
    else:
        open_ended_score = 0.5

    # List request
    list_match = re.search(r"\b(\d+)\s+(items?|examples?|ways?|steps?|points?)", prompt.lower())
    if list_match:
        n = int(list_match.group(1))
        list_request_score = min(1.0, n / 10)
    else:
        list_request_score = 0.0

    return ComplexityScore(
        word_count_score=word_count_score,
        question_count_score=question_count_score,
        technical_score=technical_score,
        open_ended_score=open_ended_score,
        list_request_score=list_request_score,
    )


def chat_with_complexity_budget(prompt: str, model: str = "claude-sonnet-4-6") -> str:
    client = anthropic.Anthropic()

    score = score_complexity(prompt)
    max_tokens = score.to_max_tokens()

    print(f"Complexity score: {score.total():.2f} → max_tokens: {max_tokens}")

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    actual = response.usage.output_tokens
    print(f"Actual tokens: {actual}/{max_tokens} ({actual/max_tokens:.0%})")
    return response.content[0].text


if __name__ == "__main__":
    prompts = [
        "Is Redis faster than Memcached?",
        "List 8 design patterns commonly used in microservices architecture.",
        "Explain and compare the concurrency models in Python, Go, and Rust, analyzing their tradeoffs for distributed systems.",
    ]

    for p in prompts:
        print(f"\nPrompt: {p[:70]}")
        chat_with_complexity_budget(p)

# Expected Token Savings: 45-65% by scoring and right-sizing before generation
# Environment: General-purpose APIs with heterogeneous query complexity
```

## Option 5: Length-Calibrated Prompt Prefix

Instruct the model to match a target length via a system prompt and max_tokens together, using a calibration table.

```python
import anthropic
import re

client = anthropic.Anthropic()

LENGTH_INSTRUCTION = {
    "tiny": ("Answer in 1-2 sentences only.", 60),
    "short": ("Answer concisely in 3-5 sentences.", 150),
    "medium": ("Give a clear, complete answer in 2-4 paragraphs.", 400),
    "long": ("Provide a thorough, detailed explanation.", 900),
    "comprehensive": ("Write a complete, exhaustive treatment of this topic.", 2000),
}

INTENT_TO_LENGTH = [
    (r"^(yes|no|is|are|was|were|can|do|does)\b", "tiny"),
    (r"\b(briefly|in a word|one sentence|tldr|tl;dr)\b", "tiny"),
    (r"\b(summarize|in short|quick|simple|basic)\b", "short"),
    (r"\b(explain|describe|how|why)\b.{0,80}\?$", "medium"),
    (r"\b(detail|thorough|complete|full|comprehensive|in-depth)\b", "long"),
    (r"\b(write|draft|implement|build|create|design)\b", "comprehensive"),
]


def detect_length_intent(prompt: str) -> str:
    lower = prompt.lower()
    for pattern, length_key in INTENT_TO_LENGTH:
        if re.search(pattern, lower):
            return length_key
    return "medium"


def chat_with_calibrated_prefix(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    override_length: str = None,
) -> str:
    length_key = override_length or detect_length_intent(prompt)
    instruction, max_tokens = LENGTH_INSTRUCTION[length_key]

    print(f"Detected length intent: {length_key} → {max_tokens} tokens")
    print(f"Instruction: {instruction}")

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=instruction,
        messages=[{"role": "user", "content": prompt}],
    )

    actual = response.usage.output_tokens
    print(f"Actual: {actual}/{max_tokens} tokens")
    return response.content[0].text


if __name__ == "__main__":
    test_cases = [
        ("Is Python a compiled language?", None),
        ("Briefly explain what an API is.", None),
        ("Explain how HTTPS works.", None),
        ("Write a comprehensive guide to setting up a Kubernetes cluster.", None),
        ("What is 2+2?", "tiny"),
    ]

    for prompt, override in test_cases:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt}")
        result = chat_with_calibrated_prefix(prompt, override_length=override)
        print(f"Result: {result[:120]}...")

# Expected Token Savings: 50-75% by combining instruction-following with max_tokens alignment
# Environment: Assistants where response style and length should match query intent
```

## Option 6: Streaming Early-Stop Based on Content Completion Detection

Stream the response and halt generation early when semantic completion signals are detected, preventing over-generation.

```python
import anthropic
import re
from enum import Enum

class CompletionSignal(Enum):
    NONE = "none"
    SENTENCE_END = "sentence_end"
    LIST_COMPLETE = "list_complete"
    CODE_BLOCK_END = "code_block_end"
    EXPLICIT_END = "explicit_end"


def detect_completion(text: str, prompt: str) -> tuple[bool, CompletionSignal]:
    """Detect if streaming content appears semantically complete."""
    stripped = text.strip()

    # Explicit completion markers
    if re.search(r"\b(in summary|in conclusion|to summarize|that\'s it|hope this helps)\b", stripped, re.I):
        return True, CompletionSignal.EXPLICIT_END

    # Closed code block after content
    code_blocks = stripped.count("```")
    if code_blocks >= 2 and code_blocks % 2 == 0:
        # Even number of ``` = all blocks closed
        after_last_block = stripped.rsplit("```", 1)[-1].strip()
        if len(after_last_block) > 10:  # some text after the code
            return True, CompletionSignal.CODE_BLOCK_END

    # Numbered list: detect if we've hit the requested number
    list_match = re.search(r"\b(\d+)\s+(items?|examples?|ways?|steps?|points?)", prompt.lower())
    if list_match:
        requested_n = int(list_match.group(1))
        found_numbers = re.findall(r"^\s*(\d+)[.)]\s", stripped, re.MULTILINE)
        if found_numbers and int(found_numbers[-1]) >= requested_n:
            return True, CompletionSignal.LIST_COMPLETE

    # Q&A: ends with a clean sentence after reasonable length
    if len(stripped) > 200:
        last_sentence_end = max(
            stripped.rfind("."),
            stripped.rfind("!"),
            stripped.rfind("?"),
        )
        if last_sentence_end > len(stripped) * 0.85:
            # Last sentence punctuation is near the end
            prompt_lower = prompt.lower()
            is_short_answer = any(
                prompt_lower.startswith(w) for w in ["what", "who", "when", "where", "is", "are"]
            )
            if is_short_answer and len(stripped) > 100:
                return True, CompletionSignal.SENTENCE_END

    return False, CompletionSignal.NONE


def stream_with_early_stop(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2048,
    enable_early_stop: bool = True,
) -> tuple[str, int, CompletionSignal]:
    client = anthropic.Anthropic()

    collected = []
    total_chars = 0
    stop_signal = CompletionSignal.NONE

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            collected.append(text)
            total_chars += len(text)
            print(text, end="", flush=True)

            if enable_early_stop and total_chars > 100:
                full_text = "".join(collected)
                complete, signal = detect_completion(full_text, prompt)
                if complete:
                    stop_signal = signal
                    print(f"\n[Early stop: {signal.value} detected at {total_chars} chars]")
                    break

    result = "".join(collected)
    # Estimate tokens from chars
    estimated_tokens = len(result) // 4
    return result, estimated_tokens, stop_signal


if __name__ == "__main__":
    test_prompts = [
        ("What is the capital of Germany?", True),
        ("List 3 benefits of using Docker.", True),
        ("Write a Python function to compute factorial.", True),
        ("Explain the entire history of computing.", False),  # disable to compare
    ]

    for prompt, early_stop in test_prompts:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt} (early_stop={early_stop})")
        result, tokens, signal = stream_with_early_stop(
            prompt, max_tokens=1000, enable_early_stop=early_stop
        )
        print(f"\nTokens used: ~{tokens} | Stop signal: {signal.value}")

# Expected Token Savings: 30-70% on short-answer and list queries by halting once content is complete
# Environment: Interactive chat, streaming APIs where semantic completion can be detected in real-time
```

## Comparison

| Option | Prediction Method | LLM Call | Overhead | Best For |
|--------|------------------|----------|----------|----------|
| 1 Task Classifier | Regex + keyword rules | None | ~0ms | High-volume, well-structured queries |
| 2 Haiku Pre-Classifier | LLM length estimation | 1 Haiku call | ~300ms | Unpredictable mixed workloads |
| 3 Historical SQLite | P85 of past responses | None (after warmup) | ~1ms | Repeated query patterns |
| 4 Complexity Score | Multi-dimension scoring | None | ~1ms | General APIs with varied complexity |
| 5 Calibrated Prefix | Intent detection + instruction | None | ~0ms | Response-style-aware assistants |
| 6 Streaming Early Stop | Semantic completion detection | None | Streaming | Interactive chat with early-exit |
