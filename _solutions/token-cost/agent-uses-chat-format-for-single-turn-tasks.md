---
layout: solution
title: "Agent Uses Chat Format for Single-Turn Tasks"
category: token-cost
description: "Agent wraps simple extraction, classification, or transformation tasks in a full multi-turn chat loop — with tool use overhead, multi-message history, and assistant prefill — when a single direct completion call would produce the same result at 40-60% lower cost."
tags: [token-cost, efficiency, single-turn, completion, prompt-engineering]
---

## Symptom

An agent classifies a support ticket's sentiment using a full agentic loop:

```python
messages = [{"role": "user", "content": "Classify sentiment: 'Your service is terrible'"}]
# → model turn 1: "I'll analyze the sentiment..."
# → tool call: classify_sentiment("Your service is terrible")
# → tool result: "negative"
# → model turn 2: "The sentiment of this message is negative."
# Total: 4 turns, ~800 tokens for a task that needs 50
```

The same task as a direct completion: `"Negative"` — 5 tokens.

## Root Cause

A general-purpose agentic scaffold is applied to every task regardless of complexity. The tool loop, multi-turn history, and streaming infrastructure add overhead that exceeds the task itself:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: agentic loop for a simple classification
def classify_sentiment(text: str) -> str:
    messages = [{"role": "user", "content": f"Classify sentiment: {text}"}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[sentiment_tool],  # Tool that just calls an API
            messages=messages
        )
        # ... full loop overhead for a 1-word answer
```

---

## Fix

### Option 1 — Direct completion call: no tools, no loop

For classification, extraction, and transformation tasks, use a single `messages.create()` call with no tools.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def classify_sentiment_direct(text: str) -> str:
    """Single-turn sentiment classification — no loop, no tools."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Cheaper model for simple tasks
        max_tokens=10,  # Tight token budget — we need one word
        system="Classify the sentiment of the text. Reply with exactly one word: Positive, Negative, or Neutral.",
        messages=[{"role": "user", "content": text}]
    )
    return response.content[0].text.strip()


def classify_batch(texts: list[str]) -> list[str]:
    """Classify multiple texts — one API call each, no overhead."""
    return [classify_sentiment_direct(t) for t in texts]


# Compare token usage
texts = [
    "Your product saved my business!",
    "This is the worst experience I've ever had.",
    "The package arrived on time.",
]

results = classify_batch(texts)
for text, result in zip(texts, results):
    print(f"[{result}] {text[:50]}")

# Token estimate per call: ~30 input + 2 output = 32 tokens
# vs agentic loop: ~300+ tokens per call
# Savings: ~90% per classification call

# Expected Token Savings: 90% reduction on simple classification; 100x faster at high volume
# Environment: batch classification, sentiment, intent detection, entity tagging pipelines
```

---

### Option 2 — Task router: send simple tasks to direct completion, complex to agentic loop

Classify incoming tasks by complexity and route them to the appropriate execution path.

```python
import anthropic
import json
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Patterns that indicate single-turn tasks
SINGLE_TURN_INDICATORS = [
    r'\b(classify|categorize|label|tag)\b',
    r'\b(extract|pull out|find|identify)\b.*\b(from|in)\b',
    r'\b(translate|convert|transform)\b',
    r'\b(summarize|summarise) in \d+ word',
    r'\b(true or false|yes or no|positive or negative)\b',
    r'\bis this\b.*\?$',
]

# Patterns that indicate multi-turn/agentic tasks
AGENTIC_INDICATORS = [
    r'\b(and then|after that|once you have)\b',
    r'\b(check|verify|confirm) (if|that|whether)\b',
    r'\b(search|look up|find out)\b',
    r'\b(create|build|write|implement)\b',
    r'\b(compare|analyse|analyze) (multiple|several|different)\b',
]


def estimate_complexity(task: str) -> str:
    task_lower = task.lower()

    agentic_score = sum(
        1 for p in AGENTIC_INDICATORS if re.search(p, task_lower)
    )
    single_score = sum(
        1 for p in SINGLE_TURN_INDICATORS if re.search(p, task_lower)
    )

    if agentic_score > single_score:
        return "agentic"
    if single_score > 0 and len(task) < 300:
        return "single_turn"
    return "agentic"  # Default to agentic when uncertain


def run_single_turn(task: str) -> str:
    """Fast direct completion — no tools, tight token budget."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="Answer concisely and directly. No preamble or explanation.",
        messages=[{"role": "user", "content": task}]
    )
    return response.content[0].text.strip()


def run_agentic(task: str) -> str:
    """Full agentic loop for complex tasks."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": task}]
    )
    return response.content[0].text.strip()


def smart_route(task: str) -> dict:
    complexity = estimate_complexity(task)
    print(f"[router] '{task[:50]}' → {complexity}")

    if complexity == "single_turn":
        result = run_single_turn(task)
        return {"result": result, "path": "single_turn", "model": "haiku"}
    else:
        result = run_agentic(task)
        return {"result": result, "path": "agentic", "model": "sonnet"}


tasks = [
    "Classify this as spam or not spam: 'Win a free iPhone now!'",
    "Extract all email addresses from: 'Contact alice@example.com or bob@corp.io'",
    "Is this sentence grammatically correct: 'He go to store'?",
    "Search for the latest pricing of AWS EC2 instances, compare them, and recommend the best one for a web server handling 1000 requests/minute",
    "Translate 'Hello world' to Spanish",
]

for task in tasks:
    output = smart_route(task)
    print(f"  Path: {output['path']} | Result: {output['result'][:60]}\n")

# Expected Token Savings: simple tasks use 10% of agentic tokens; routing adds negligible overhead
# Environment: mixed-complexity workloads; customer support; content moderation pipelines
```

---

### Option 3 — Batch single-turn tasks in one API call using JSON output

Instead of one API call per item, batch multiple single-turn tasks into one call and get results as a JSON array.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def batch_classify(texts: list[str], task_description: str) -> list[dict]:
    """
    Classify multiple texts in a single API call.
    Returns list of {index, text, result} dicts.
    """
    if not texts:
        return []

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(texts) * 20 + 50,  # Budget per item
        system=f"""You are performing batch classification.
Task: {task_description}

Return a JSON array with one object per input:
[{{"index": 1, "result": "..."}}]

Return ONLY the JSON array. No explanation.""",
        messages=[{"role": "user", "content": f"Classify these:\n{numbered}"}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        results = json.loads(raw)
        return [
            {"index": r["index"], "text": texts[r["index"] - 1], "result": r["result"]}
            for r in results
        ]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"[batch] Parse error: {e} — raw: {raw[:100]}")
        return [{"index": i + 1, "text": t, "result": "unknown"} for i, t in enumerate(texts)]


# 10 items in 1 API call vs 10 separate calls
reviews = [
    "Absolutely love this product!",
    "Terrible quality, broke in a week.",
    "Works as expected, nothing special.",
    "Best purchase I've made this year!",
    "Disappointed — doesn't match description.",
    "Great value for money.",
    "Returned immediately. Awful.",
    "Decent product for the price.",
    "Exceeded my expectations!",
    "Complete waste of money.",
]

results = batch_classify(
    reviews,
    "Classify each review as: Positive, Negative, or Neutral"
)

for r in results:
    print(f"[{r['result']:8s}] {r['text'][:50]}")

# Expected Token Savings: 1 API call vs 10; ~3x token savings from shared context
# Environment: bulk classification, sentiment analysis, content moderation at scale
```

---

### Option 4 — Structured extraction without tools: JSON output mode

For data extraction tasks, use `max_tokens` + JSON system prompt instead of tool calls. Faster, cheaper, no tool overhead.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_entities(text: str) -> dict:
    """Extract structured entities from text — no tools needed."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="""Extract entities from the text and return as JSON:
{
  "people": ["list of person names"],
  "organizations": ["list of org names"],
  "locations": ["list of place names"],
  "dates": ["list of date mentions"],
  "amounts": ["list of monetary amounts or quantities"]
}

Return ONLY valid JSON. Use empty arrays for categories with no entities found.""",
        messages=[{"role": "user", "content": text}]
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Failed to parse entity extraction result", "raw": raw}


def extract_invoice_data(invoice_text: str) -> dict:
    """Extract structured invoice fields — single call, no tool loop."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="""Extract invoice data and return JSON:
{
  "invoice_number": "...",
  "vendor": "...",
  "amount": 0.00,
  "currency": "USD",
  "due_date": "YYYY-MM-DD or null",
  "line_items": [{"description": "...", "amount": 0.00}]
}""",
        messages=[{"role": "user", "content": invoice_text}]
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {"error": "Parse failed", "raw_preview": raw[:100]}


# Entity extraction
article = "On March 15, Apple CEO Tim Cook met with Microsoft's Satya Nadella in Seattle to discuss a $500M partnership."
entities = extract_entities(article)
print("Entities:", json.dumps(entities, indent=2))

# Invoice extraction
invoice = "Invoice #INV-2026-0042 from Acme Corp. Total: $1,250.00 USD due by 2026-05-01. Items: Software license $1,000, Support $250."
data = extract_invoice_data(invoice)
print("\nInvoice:", json.dumps(data, indent=2))

# Expected Token Savings: JSON output without tools saves tool_use + tool_result overhead (~200 tokens/call)
# Environment: document parsing, invoice processing, NER pipelines, form extraction
```

---

### Option 5 — Task complexity estimator: token budget prediction before calling

Estimate the token budget required for a task before choosing single-turn vs agentic. Use the budget to select model and approach.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def estimate_output_tokens(task: str) -> int:
    """Heuristic estimate of output tokens needed for a task."""
    task_lower = task.lower()

    # Extract explicit length constraints
    word_match = re.search(r'in (\d+) word', task_lower)
    if word_match:
        return int(word_match.group(1)) * 2  # ~1.5 tokens/word

    sentence_match = re.search(r'in (\d+) sentence', task_lower)
    if sentence_match:
        return int(sentence_match.group(1)) * 25  # ~25 tokens/sentence

    # Category-based estimates
    if any(k in task_lower for k in ["classify", "label", "is this", "yes or no", "true or false"]):
        return 5
    if any(k in task_lower for k in ["extract", "identify all", "list all"]):
        return min(len(task) // 2, 200)
    if any(k in task_lower for k in ["translate", "convert"]):
        return len(task) + 50
    if any(k in task_lower for k in ["summarize", "summarise", "brief"]):
        return 150
    if any(k in task_lower for k in ["explain", "describe", "how does"]):
        return 300
    if any(k in task_lower for k in ["write", "create", "implement", "build"]):
        return 1000
    if any(k in task_lower for k in ["analyse", "analyze", "research", "compare"]):
        return 800

    return 400  # Default estimate


def select_execution_plan(task: str) -> dict:
    """Select model, max_tokens, and approach based on task complexity."""
    estimated_tokens = estimate_output_tokens(task)
    task_length = len(task)

    if estimated_tokens <= 20 and task_length < 500:
        return {"model": "claude-haiku-4-5-20251001", "max_tokens": 30, "approach": "direct", "use_tools": False}
    elif estimated_tokens <= 300 and task_length < 1000:
        return {"model": "claude-haiku-4-5-20251001", "max_tokens": estimated_tokens + 50, "approach": "direct", "use_tools": False}
    elif estimated_tokens <= 800:
        return {"model": "claude-sonnet-4-6", "max_tokens": estimated_tokens + 100, "approach": "direct", "use_tools": False}
    else:
        return {"model": "claude-sonnet-4-6", "max_tokens": estimated_tokens + 200, "approach": "agentic", "use_tools": True}


def execute_with_plan(task: str) -> dict:
    plan = select_execution_plan(task)
    print(f"[plan] {plan}")

    response = client.messages.create(
        model=plan["model"],
        max_tokens=plan["max_tokens"],
        system="Answer directly and concisely." if not plan["use_tools"] else "Use tools as needed.",
        messages=[{"role": "user", "content": task}]
    )

    return {
        "result": response.content[0].text.strip() if response.content else "",
        "plan": plan,
        "tokens_used": response.usage.input_tokens + response.usage.output_tokens
    }


tasks = [
    "Is 'Python' a programming language? Answer yes or no.",
    "Summarize the key points of machine learning in 2 sentences.",
    "Translate 'Good morning' to French, Spanish, and Japanese.",
    "Write a comprehensive guide to building a REST API with FastAPI.",
]

for task in tasks:
    output = execute_with_plan(task)
    print(f"Task: {task[:60]}")
    print(f"Used: {output['tokens_used']} tokens | Model: {output['plan']['model']}")
    print(f"Result: {output['result'][:80]}\n")

# Expected Token Savings: right-sizing avoids 800-token claude-sonnet-4-6 calls for 5-token answers
# Environment: mixed-complexity agents; cost-sensitive production deployments
```

---

### Option 6 — Prompt template cache: reuse compiled prompts for recurring single-turn tasks

For single-turn tasks run repeatedly (e.g., classification, sentiment), cache the system prompt and reuse it across calls.

```python
import anthropic
import time
from functools import lru_cache

client = anthropic.Anthropic(api_key="sk-live-...")

# Predefined task templates — system prompts are static and cache-eligible
TASK_TEMPLATES = {
    "sentiment": {
        "system": "Classify sentiment as Positive, Negative, or Neutral. Reply with one word.",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 5,
    },
    "language_detect": {
        "system": "Detect the language of the input text. Reply with the language name only (e.g., 'English', 'French').",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 10,
    },
    "spam_check": {
        "system": "Is this message spam? Reply with 'Spam' or 'Not spam' only.",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 5,
    },
    "pii_check": {
        "system": "Does this text contain personally identifiable information (PII)? Reply 'Yes' or 'No' only.",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 5,
    },
    "intent": {
        "system": "Classify user intent as one of: Question, Complaint, Request, Compliment, Other. One word reply.",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 10,
    }
}


def run_template_task(task_name: str, text: str) -> str:
    """Run a single-turn task using a pre-defined template."""
    template = TASK_TEMPLATES.get(task_name)
    if not template:
        raise ValueError(f"Unknown task template: {task_name}")

    response = client.messages.create(
        model=template["model"],
        max_tokens=template["max_tokens"],
        system=template["system"],
        messages=[{"role": "user", "content": text}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
    )
    return response.content[0].text.strip()


def process_message_pipeline(message: str) -> dict:
    """Run all single-turn checks on a message using cached templates."""
    t0 = time.monotonic()

    results = {
        "sentiment": run_template_task("sentiment", message),
        "spam": run_template_task("spam_check", message),
        "intent": run_template_task("intent", message),
        "has_pii": run_template_task("pii_check", message),
    }
    elapsed = time.monotonic() - t0

    return {**results, "_processing_ms": round(elapsed * 1000)}


messages = [
    "I absolutely love your product! Can you tell me more about pricing?",
    "Buy now!!! Click here for FREE iPhone!!! Limited time offer!!!",
    "My email is john@example.com and my account is broken.",
]

for msg in messages:
    result = process_message_pipeline(msg)
    print(f"Message: {msg[:60]}")
    print(f"Results: {result}\n")

# Expected Token Savings: cached system prompts cost 0 on subsequent calls; template approach = 5 calls at Haiku price
# Environment: high-volume message processing pipelines; real-time content moderation; support triage
```

---

## Comparison

| Option | API Calls | Model | Overhead | Throughput |
|--------|-----------|-------|----------|------------|
| 1 | 1 per item | Haiku | Minimal | High |
| 2 | 1 (routing) | Haiku/Sonnet | Routing overhead | Very high |
| 3 | 1 for N items | Haiku | Batch overhead | Highest |
| 4 | 1 per item | Haiku | Minimal | High |
| 5 | 1 per item | Adaptive | Budget estimation | High |
| 6 | 1 per item (cached) | Haiku | None after first | Highest |

**Recommended starting point:** Option 1 (direct completion) for any classification/extraction task — remove the agentic loop entirely, write a tight system prompt, and set `max_tokens` to the minimum needed. This single change cuts token cost by 80-90% for simple tasks. Add Option 3 (batching) for high-volume pipelines to further reduce API call count.
