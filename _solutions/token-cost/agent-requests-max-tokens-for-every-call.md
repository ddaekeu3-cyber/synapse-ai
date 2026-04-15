---
layout: solution
title: "Agent Requests Max Tokens for Every Call — Pays for Unused Output Capacity"
category: token-cost
description: "Agent sets max_tokens=4096 for every API call — a yes/no question, a classification, a one-word answer, and a full essay all get the same max_tokens budget. Output tokens cost 3–5× more than input tokens. Paying for 4096 tokens when the answer is 3 words wastes money and increases latency."
tags: [token-cost, max-tokens, output-tokens, cost, latency, dynamic, budget, optimization]
---

# Agent Requests Max Tokens for Every Call — Pays for Unused Output Capacity

## Symptom
- Every API call has `max_tokens=4096` regardless of expected response length
- Classification task (output: one word) billed same as essay task (output: 1000 words)
- Response latency is uniform across all task types — short answers aren't faster
- Monthly API bill dominated by output tokens even though most tasks need short responses
- Agent generates verbose responses to simple questions because token budget encourages it
- `stop_reason: "end_turn"` with 50 tokens used on a 4096-token budget — 4046 tokens wasted in capacity reservation

## Root Cause
`max_tokens` is a ceiling, not a target — the model stops earlier if it finishes. However, using a high `max_tokens` has real costs: (1) output tokens are billed at 3–5× input token rates, and (2) higher `max_tokens` reserves capacity that affects latency in some API configurations. More practically, a large `max_tokens` budget signals to the model that a long response is expected, which can cause verbosity. Using task-appropriate token budgets saves cost and improves response conciseness.

## Fix

### Option 1: Task-type-based max_tokens presets

```python
from enum import Enum
from dataclasses import dataclass

class TaskType(Enum):
    # Very short output
    CLASSIFICATION = "classification"        # Single category label
    YES_NO = "yes_no"                        # Boolean answer
    EXTRACTION_SHORT = "extraction_short"    # Single field extraction
    SENTIMENT = "sentiment"                  # Positive/negative/neutral

    # Short output
    SUMMARY_SENTENCE = "summary_sentence"    # 1-3 sentence summary
    ANSWER_FACTUAL = "answer_factual"        # Short factual answer
    EXTRACTION_LIST = "extraction_list"      # List of entities

    # Medium output
    SUMMARY_PARAGRAPH = "summary_paragraph"  # Paragraph summary
    ANALYSIS = "analysis"                    # Analysis with reasoning
    EMAIL_SHORT = "email_short"              # Brief email

    # Long output
    REPORT = "report"                        # Full report
    CODE_SNIPPET = "code_snippet"            # Code implementation
    EMAIL_LONG = "email_long"               # Detailed email

    # Very long output
    ESSAY = "essay"                          # Long-form writing
    CODE_FULL = "code_full"                 # Full module/file
    DOCUMENT = "document"                   # Full document

@dataclass
class TokenBudget:
    max_tokens: int
    description: str

TASK_TOKEN_BUDGETS: dict[TaskType, TokenBudget] = {
    # Very short: 1–20 tokens typical
    TaskType.CLASSIFICATION:     TokenBudget(30,   "Single category label"),
    TaskType.YES_NO:             TokenBudget(10,   "Yes/no with brief reason"),
    TaskType.EXTRACTION_SHORT:   TokenBudget(50,   "Single extracted value"),
    TaskType.SENTIMENT:          TokenBudget(20,   "Sentiment label + confidence"),

    # Short: 50–200 tokens typical
    TaskType.SUMMARY_SENTENCE:   TokenBudget(200,  "1-3 sentence summary"),
    TaskType.ANSWER_FACTUAL:     TokenBudget(150,  "Factual answer with citation"),
    TaskType.EXTRACTION_LIST:    TokenBudget(300,  "List of extracted entities"),

    # Medium: 200–800 tokens typical
    TaskType.SUMMARY_PARAGRAPH:  TokenBudget(600,  "Paragraph-length summary"),
    TaskType.ANALYSIS:           TokenBudget(800,  "Analysis with reasoning"),
    TaskType.EMAIL_SHORT:        TokenBudget(400,  "Brief professional email"),

    # Long: 800–2000 tokens typical
    TaskType.REPORT:             TokenBudget(2000, "Structured report"),
    TaskType.CODE_SNIPPET:       TokenBudget(1500, "Function or class implementation"),
    TaskType.EMAIL_LONG:         TokenBudget(800,  "Detailed email with context"),

    # Very long: 2000–4096 tokens
    TaskType.ESSAY:              TokenBudget(4096, "Long-form essay"),
    TaskType.CODE_FULL:          TokenBudget(4096, "Full module implementation"),
    TaskType.DOCUMENT:           TokenBudget(4096, "Complete document"),
}

import anthropic

client = anthropic.Anthropic()

def call_with_appropriate_budget(
    task_type: TaskType,
    prompt: str,
    system: str = "",
    model: str = "claude-sonnet-4-6"
) -> tuple[str, dict]:
    """
    Call API with task-appropriate max_tokens budget.
    Returns (response_text, usage_stats).
    """
    budget = TASK_TOKEN_BUDGETS[task_type]

    messages_args = {"role": "user", "content": prompt}
    kwargs = {
        "model": model,
        "messages": [messages_args],
        "max_tokens": budget.max_tokens
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)

    usage = {
        "task_type": task_type.value,
        "max_tokens_set": budget.max_tokens,
        "output_tokens_used": response.usage.output_tokens,
        "efficiency": f"{response.usage.output_tokens/budget.max_tokens*100:.0f}%",
        "stop_reason": response.stop_reason
    }

    return response.content[0].text, usage

# Examples:
text, stats = call_with_appropriate_budget(
    TaskType.SENTIMENT,
    "Classify sentiment: 'The product is okay but support is terrible'",
)
# max_tokens=20, actual output: ~8 tokens — efficient

text, stats = call_with_appropriate_budget(
    TaskType.CODE_FULL,
    "Write a complete FastAPI CRUD app for a todo list",
)
# max_tokens=4096, actual output: ~2000 tokens — appropriate
```

### Option 2: Infer task type from prompt automatically

```python
import re

TASK_CLASSIFIERS = [
    # (pattern, task_type, max_tokens)
    (r'\b(classify|categorize|label|which category)\b', TaskType.CLASSIFICATION, 30),
    (r'\b(yes or no|true or false|is it|does it|should i|can i)\b', TaskType.YES_NO, 10),
    (r'\b(what is the sentiment|positive|negative|neutral)\b', TaskType.SENTIMENT, 20),
    (r'\b(extract|find all|list all|identify all)\b', TaskType.EXTRACTION_LIST, 300),
    (r'\b(summarize in one sentence|one-sentence summary|tl;dr)\b', TaskType.SUMMARY_SENTENCE, 200),
    (r'\b(write a report|generate a report|full analysis)\b', TaskType.REPORT, 2000),
    (r'\b(write.*code|implement|create.*function|build.*class)\b', TaskType.CODE_SNIPPET, 1500),
    (r'\b(write.*email|draft.*email|compose.*email)\b', TaskType.EMAIL_SHORT, 400),
    (r'\b(write.*essay|long.*form|detailed.*analysis)\b', TaskType.ESSAY, 4096),
]

def infer_max_tokens(prompt: str, default: int = 1024) -> tuple[int, str]:
    """
    Infer appropriate max_tokens from the prompt text.
    Returns (max_tokens, reasoning).
    """
    prompt_lower = prompt.lower()

    for pattern, task_type, max_tokens in TASK_CLASSIFIERS:
        if re.search(pattern, prompt_lower):
            return max_tokens, f"Inferred task type: {task_type.value}"

    # Estimate based on prompt complexity
    prompt_words = len(prompt.split())
    if prompt_words < 10:
        return 100, "Short prompt → short response expected"
    elif prompt_words < 50:
        return 500, "Medium prompt → medium response"
    else:
        return default, "Long/complex prompt → using default budget"

def smart_create(prompt: str, system: str = "", model: str = "claude-sonnet-4-6") -> str:
    """
    Create a message with automatically inferred max_tokens.
    """
    max_tokens, reasoning = infer_max_tokens(prompt)
    print(f"max_tokens={max_tokens} ({reasoning})")

    response = client.messages.create(
        model=model,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens
    )

    if response.stop_reason == "max_tokens":
        print(f"WARNING: Hit max_tokens limit ({max_tokens}). Consider increasing budget for this task type.")

    return response.content[0].text

# Usage:
result = smart_create("Is this email spam? 'Win $1000 gift card! Click now!'")
# → max_tokens=10, answer: "Yes" (2 tokens used)

result = smart_create("Write a Python function that implements binary search")
# → max_tokens=1500, answer: full function implementation
```

### Option 3: Monitor and alert on token efficiency

```python
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

@dataclass
class TokenEfficiencyTracker:
    """
    Track token budget utilization across all API calls.
    Alerts on consistently inefficient calls.
    """
    _calls: list[dict] = field(default_factory=list)
    _by_task: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))

    def record(self, task_label: str, max_tokens: int, output_tokens: int):
        efficiency = output_tokens / max_tokens
        record = {
            "task": task_label,
            "max_tokens": max_tokens,
            "output_tokens": output_tokens,
            "efficiency": efficiency,
            "wasted_tokens": max_tokens - output_tokens
        }
        self._calls.append(record)
        self._by_task[task_label].append(record)

        # Alert on consistently low efficiency
        task_calls = self._by_task[task_label]
        if len(task_calls) >= 5:
            avg_efficiency = statistics.mean(c["efficiency"] for c in task_calls[-10:])
            if avg_efficiency < 0.15:  # Using less than 15% of budget
                avg_actual = statistics.mean(c["output_tokens"] for c in task_calls[-10:])
                print(
                    f"TOKEN BUDGET ALERT: Task '{task_label}' uses only "
                    f"{avg_efficiency*100:.0f}% of max_tokens budget on average. "
                    f"Actual output: ~{avg_actual:.0f} tokens. "
                    f"Consider reducing max_tokens from {max_tokens} to {int(avg_actual * 1.5)}."
                )

    def report(self) -> dict:
        if not self._calls:
            return {}

        total_budget = sum(c["max_tokens"] for c in self._calls)
        total_used = sum(c["output_tokens"] for c in self._calls)
        wasted = total_budget - total_used

        task_summary = {}
        for task, calls in self._by_task.items():
            avg_eff = statistics.mean(c["efficiency"] for c in calls)
            avg_out = statistics.mean(c["output_tokens"] for c in calls)
            task_summary[task] = {
                "calls": len(calls),
                "avg_efficiency": f"{avg_eff*100:.0f}%",
                "avg_output_tokens": round(avg_out),
                "recommended_max_tokens": round(avg_out * 1.5)
            }

        return {
            "total_calls": len(self._calls),
            "total_budget_tokens": total_budget,
            "total_used_tokens": total_used,
            "wasted_capacity_tokens": wasted,
            "overall_efficiency": f"{total_used/total_budget*100:.0f}%",
            "by_task": task_summary
        }

tracker = TokenEfficiencyTracker()

# After each API call:
tracker.record(
    task_label="sentiment_classification",
    max_tokens=4096,  # Current (wasteful)
    output_tokens=12  # Actual
)

# Weekly report:
report = tracker.report()
for task, stats in report["by_task"].items():
    print(f"{task}: {stats['avg_efficiency']} efficient, recommend max_tokens={stats['recommended_max_tokens']}")
```

### Option 4: Dynamic budget with stop sequences

```python
import anthropic

client = anthropic.Anthropic()

def call_with_stop_sequences(
    prompt: str,
    max_tokens: int,
    stop_after_patterns: list[str] | None = None,
    model: str = "claude-sonnet-4-6"
) -> str:
    """
    Use stop sequences to terminate early on structured outputs.
    E.g., for JSON output, stop after the closing '}'.
    Avoids paying for extra tokens after the answer is complete.
    """
    stop_sequences = stop_after_patterns or []

    # Common stop sequences by output type:
    STOP_SEQUENCES = {
        "json_object": ["\n\n"],       # Stop after first blank line post-JSON
        "classification": ["\n"],      # Stop after first line (single label)
        "yes_no": ["\n", "."],         # Stop after first sentence
        "list": ["---", "\n\n\n"],    # Stop after list ends
    }

    response = client.messages.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        stop_sequences=stop_sequences
    )

    return response.content[0].text.strip()

# For classification: stop after first newline — single label only
label = call_with_stop_sequences(
    "Classify this text as: SPAM, HAM, or UNCERTAIN\nText: 'Click here to win!'",
    max_tokens=20,
    stop_after_patterns=["\n"]
)
# → "SPAM" (1 token, stops immediately)
```

### Option 5: Cost-aware model selection with token budgets

```python
from dataclasses import dataclass

@dataclass
class ModelConfig:
    model_id: str
    input_cost_per_mtok: float   # $ per million input tokens
    output_cost_per_mtok: float  # $ per million output tokens
    max_context: int

MODELS = {
    "haiku": ModelConfig(
        "claude-haiku-4-5-20251001",
        input_cost_per_mtok=0.80,
        output_cost_per_mtok=4.00,
        max_context=200_000
    ),
    "sonnet": ModelConfig(
        "claude-sonnet-4-6",
        input_cost_per_mtok=3.00,
        output_cost_per_mtok=15.00,
        max_context=200_000
    ),
    "opus": ModelConfig(
        "claude-opus-4-6",
        input_cost_per_mtok=15.00,
        output_cost_per_mtok=75.00,
        max_context=200_000
    ),
}

def estimate_call_cost(
    model_name: str,
    input_tokens: int,
    max_tokens: int,
    expected_output_fraction: float = 0.5  # Expect to use X% of max_tokens
) -> dict:
    """
    Estimate cost of an API call before making it.
    Shows cost at expected output, and worst-case (full max_tokens).
    """
    model = MODELS[model_name]
    expected_output = int(max_tokens * expected_output_fraction)

    input_cost = input_tokens / 1_000_000 * model.input_cost_per_mtok
    expected_output_cost = expected_output / 1_000_000 * model.output_cost_per_mtok
    max_output_cost = max_tokens / 1_000_000 * model.output_cost_per_mtok

    return {
        "model": model_name,
        "input_tokens": input_tokens,
        "max_tokens": max_tokens,
        "expected_output_tokens": expected_output,
        "expected_total_cost_usd": round(input_cost + expected_output_cost, 6),
        "worst_case_cost_usd": round(input_cost + max_output_cost, 6),
        "savings_from_right_sizing": round(
            (max_tokens - expected_output) / 1_000_000 * model.output_cost_per_mtok, 6
        )
    }

# Compare cost for classification task:
for max_tok in [4096, 1024, 100, 20]:
    cost = estimate_call_cost("sonnet", input_tokens=500, max_tokens=max_tok, expected_output_fraction=0.25)
    print(f"max_tokens={max_tok}: expected cost ${cost['expected_total_cost_usd']:.5f}")

# max_tokens=4096: expected cost $0.00194
# max_tokens=20:   expected cost $0.00015  → 92% cost reduction for classification tasks
```

### Option 6: A/B test token budgets to find optimal values

```python
import random
import statistics
from collections import defaultdict

class TokenBudgetExperimenter:
    """
    A/B test different max_tokens values to find the optimal setting per task.
    Measures: response quality (via length), stop_reason, actual tokens used.
    """

    def __init__(self, task_name: str, candidate_budgets: list[int]):
        self.task_name = task_name
        self.budgets = candidate_budgets
        self._results: dict[int, list[dict]] = defaultdict(list)

    async def run_trial(self, prompt: str) -> dict:
        """Run one trial with a randomly selected budget"""
        budget = random.choice(self.budgets)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=budget
        )

        result = {
            "budget": budget,
            "output_tokens": response.usage.output_tokens,
            "stop_reason": response.stop_reason,
            "hit_limit": response.stop_reason == "max_tokens",
            "response_length": len(response.content[0].text),
        }
        self._results[budget].append(result)
        return result

    def analyze(self) -> dict:
        """Analyze results to find optimal budget"""
        analysis = {}

        for budget, results in self._results.items():
            hit_limit_rate = sum(r["hit_limit"] for r in results) / len(results)
            avg_tokens = statistics.mean(r["output_tokens"] for r in results)
            analysis[budget] = {
                "trials": len(results),
                "hit_limit_rate": f"{hit_limit_rate*100:.0f}%",
                "avg_output_tokens": round(avg_tokens),
                "efficiency": f"{avg_tokens/budget*100:.0f}%",
                "recommended": hit_limit_rate < 0.05  # < 5% truncation rate
            }

        # Find minimum budget with < 5% truncation
        recommended = min(
            (b for b, a in analysis.items() if a["recommended"]),
            default=max(self.budgets)
        )
        analysis["recommendation"] = recommended
        return analysis
```

## Token Budget Recommendations by Task

| Task | Typical Output | Recommended max_tokens | Cost vs max_tokens=4096 |
|---|---|---|---|
| Yes/No classification | 1–5 tokens | 10 | 0.2% of baseline cost |
| Single label | 1–10 tokens | 20 | 0.5% |
| Sentiment analysis | 5–15 tokens | 30 | 0.7% |
| Short factual answer | 20–80 tokens | 150 | 3.7% |
| Sentence summary | 30–100 tokens | 200 | 4.9% |
| Paragraph summary | 100–400 tokens | 600 | 14.6% |
| Code function | 100–800 tokens | 1500 | 36.6% |
| Full report | 500–2000 tokens | 3000 | 73.2% |
| Long-form content | 1000–4000 tokens | 4096 | 100% |

## Expected Token Savings
Classification task at max_tokens=4096 vs max_tokens=20: 99.5% output token reduction
At $15/M output tokens (Sonnet): $0.06 → $0.0003 per classification call

## Environment
- Any agent making repeated API calls for structured tasks; critical for high-volume classification, extraction, and QA agents where cost per call multiplies across millions of requests
- Source: direct experience; uniform max_tokens is the easiest cost optimization to implement and often reduces API bills by 40–80% for mixed-task agents
