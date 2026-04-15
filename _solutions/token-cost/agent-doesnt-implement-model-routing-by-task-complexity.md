---
layout: solution
title: "Agent Doesn't Implement Model Routing by Task Complexity"
category: token-cost
description: "Agents that send every request to the most capable (and expensive) model waste 60-80% of their budget on simple tasks that a cheaper model handles just as well — complexity-based routing cuts costs without sacrificing quality."
tags: [model-routing, token-cost, cost-optimization, task-complexity, haiku, sonnet, opus]
---

# Agent Doesn't Implement Model Routing by Task Complexity

## Problem

Most agents pick one model and use it for everything. Using `claude-opus-4-6` to answer "What is 2+2?" or format a JSON object is like hiring a senior architect to hang a picture frame. Simple retrieval, formatting, classification, and short-answer tasks perform equally well on `claude-haiku-4-5-20251001` at ~30× lower cost. Complex reasoning, code generation, and multi-step planning need `claude-sonnet-4-6` or `claude-opus-4-6`. Routing requests to the right model tier based on measured complexity is the single highest-ROI cost optimization available.

## Solutions

### Option 1: Heuristic Complexity Classifier

Use fast heuristics (input length, keyword presence, task type) to classify complexity before routing — zero extra API cost.

```python
import re
import anthropic
from enum import Enum

class Complexity(Enum):
    LOW = "low"       # haiku
    MEDIUM = "medium" # sonnet
    HIGH = "high"     # opus

MODEL_MAP = {
    Complexity.LOW:    "claude-haiku-4-5-20251001",
    Complexity.MEDIUM: "claude-sonnet-4-6",
    Complexity.HIGH:   "claude-opus-4-6",
}

COST_PER_M_INPUT = {
    "claude-haiku-4-5-20251001": 0.80,
    "claude-sonnet-4-6":         3.00,
    "claude-opus-4-6":          15.00,
}

# Keywords that suggest complex reasoning
HIGH_COMPLEXITY_PATTERNS = [
    r"\b(architect|design|trade-?off|compare|evaluate|analyze|critique|optimize)\b",
    r"\b(explain why|reason about|pros and cons|implications)\b",
    r"\b(refactor|rewrite|redesign|plan)\b",
    r"\b(security|vulnerability|cve|exploit)\b",
]

MEDIUM_COMPLEXITY_PATTERNS = [
    r"\b(implement|write|create|build|generate)\b",
    r"\b(debug|fix|solve|troubleshoot)\b",
    r"\b(summarize|describe|how to)\b",
]

# Low complexity: simple lookup, format, classify, short Q&A
LOW_COMPLEXITY_PATTERNS = [
    r"^(what is|who is|when did|where is|define)\b",
    r"^(list|give me|show me)\s+\d+\s+",
    r"\b(format|convert|translate|rename)\b",
    r"\b(yes or no|true or false|is it|does it)\b",
]

def classify_complexity(user_message: str) -> Complexity:
    text = user_message.lower().strip()
    length = len(text.split())

    # Length heuristic
    if length > 200:
        return Complexity.HIGH
    if length > 80:
        return Complexity.MEDIUM

    # Pattern matching (most specific wins)
    for pattern in HIGH_COMPLEXITY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return Complexity.HIGH

    for pattern in LOW_COMPLEXITY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return Complexity.LOW

    for pattern in MEDIUM_COMPLEXITY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return Complexity.MEDIUM

    # Default: medium for safety
    return Complexity.MEDIUM

client = anthropic.Anthropic()

def routed_request(user_message: str, system: str = "You are a helpful assistant.") -> dict:
    complexity = classify_complexity(user_message)
    model = MODEL_MAP[complexity]

    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    tokens = response.usage.input_tokens + response.usage.output_tokens
    cost = tokens / 1_000_000 * COST_PER_M_INPUT[model]

    return {
        "response": response.content[0].text,
        "model": model,
        "complexity": complexity.value,
        "tokens": tokens,
        "est_cost_usd": round(cost, 6),
    }

# Test routing
test_cases = [
    "What is a Python list?",
    "Write a FastAPI endpoint that validates JWT tokens.",
    "Analyze the architectural trade-offs between event-driven and request-response patterns for a high-throughput payment system.",
]

for msg in test_cases:
    result = routed_request(msg)
    print(f"[{result['complexity'].upper():6}] → {result['model'].split('-')[1]:6} | "
          f"${result['est_cost_usd']:.6f} | {msg[:60]}")
# Expected Token Savings: 60-80% on typical mixed workloads vs always using Sonnet/Opus
# Environment: Any agent handling mixed task types — chatbots, coding assistants, pipelines
```

### Option 2: LLM Pre-Classifier with Tiny Model

Use the cheapest model to classify complexity, then route to the appropriate tier. The classifier call costs almost nothing but enables precise routing.

```python
import anthropic
import json

client = anthropic.Anthropic()

CLASSIFIER_SYSTEM = """You are a task complexity classifier. Classify the user's request.

Respond with JSON only:
{"complexity": "low"|"medium"|"high", "reason": "one sentence", "estimated_tokens": N}

Guidelines:
- low: factual lookup, formatting, simple yes/no, translation, short list (<200 output tokens)
- medium: code writing, explanation, summarization, debugging (200-800 output tokens)
- high: architecture design, multi-step reasoning, security analysis, creative writing (800+ output tokens)"""

MODEL_TIERS = {
    "low":    ("claude-haiku-4-5-20251001", 256),
    "medium": ("claude-sonnet-4-6",         1024),
    "high":   ("claude-opus-4-6",           4096),
}

def classify_with_llm(user_message: str) -> dict:
    """Use haiku (cheap) to classify before routing to the right tier."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    try:
        return json.loads(resp.content[0].text)
    except json.JSONDecodeError:
        return {"complexity": "medium", "reason": "parse error", "estimated_tokens": 500}

def smart_routed_call(
    user_message: str,
    system: str = "You are a helpful assistant.",
    force_complexity: str | None = None,
) -> dict:
    # Classify (or use override)
    if force_complexity:
        classification = {"complexity": force_complexity, "reason": "forced"}
    else:
        classification = classify_with_llm(user_message)

    complexity = classification.get("complexity", "medium")
    model, max_tokens = MODEL_TIERS.get(complexity, MODEL_TIERS["medium"])

    # Execute on the routed model
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    return {
        "answer": response.content[0].text,
        "routed_to": model,
        "complexity": complexity,
        "routing_reason": classification.get("reason"),
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }

queries = [
    "Define idempotency.",
    "Write a Python class for a thread-safe LRU cache.",
    "Design a distributed rate-limiting system for 100k RPS with sub-millisecond P99 latency.",
]

for q in queries:
    result = smart_routed_call(q)
    print(f"[{result['complexity']:6}] {result['routed_to'].split('-')[1]:6} | "
          f"in={result['input_tokens']} out={result['output_tokens']} | "
          f"{q[:55]}...")
    print(f"   Reason: {result['routing_reason']}")
# Expected Token Savings: 55-75% — classifier cost ~5 tokens; savings on routed calls >> classifier cost
# Environment: High-volume production agents where routing accuracy matters
```

### Option 3: Confidence-Based Escalation

Start with the cheapest model. If the response contains uncertainty markers or the output is below a quality threshold, escalate to a stronger model automatically.

```python
import anthropic
import re

client = anthropic.Anthropic()

UNCERTAINTY_PATTERNS = [
    r"\bi('m| am) not sure\b",
    r"\bi (don't|do not) (know|have|understand)\b",
    r"\buncertain\b",
    r"\bi (can't|cannot) (determine|tell|say)\b",
    r"\byou (might|may) want to (consult|check|verify)\b",
    r"\bbeyond my (ability|capability|expertise)\b",
    r"\bi('m| am) unable to\b",
    r"\bthis is (complex|complicated|nuanced)\b",
    r"\b(further research|more information) (is )?(needed|required)\b",
]

_UNCERTAINTY_RE = re.compile("|".join(UNCERTAINTY_PATTERNS), re.IGNORECASE)

def has_uncertainty(response_text: str) -> bool:
    return bool(_UNCERTAINTY_RE.search(response_text))

def is_too_short(response_text: str, min_words: int = 30) -> bool:
    return len(response_text.split()) < min_words

ESCALATION_CHAIN = [
    ("claude-haiku-4-5-20251001", 512),
    ("claude-sonnet-4-6",         1024),
    ("claude-opus-4-6",           2048),
]

def escalating_call(
    user_message: str,
    system: str = "You are a helpful assistant.",
    min_response_words: int = 30,
) -> dict:
    history = []

    for i, (model, max_tokens) in enumerate(ESCALATION_CHAIN):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        history.append({
            "model": model,
            "tokens": response.usage.input_tokens + response.usage.output_tokens,
            "escalated": False,
        })

        # Check if we should escalate
        uncertain = has_uncertainty(text)
        too_short = is_too_short(text, min_response_words)
        is_last = i == len(ESCALATION_CHAIN) - 1

        if (uncertain or too_short) and not is_last:
            history[-1]["escalated"] = True
            history[-1]["escalation_reason"] = "uncertain" if uncertain else "too_short"
            continue  # Escalate to next tier

        # Accept this response
        return {
            "answer": text,
            "final_model": model,
            "escalation_chain": history,
            "total_tokens": sum(h["tokens"] for h in history),
            "escalations": sum(1 for h in history if h.get("escalated")),
        }

    # Fallback (shouldn't reach here)
    return {"answer": "", "final_model": ESCALATION_CHAIN[-1][0], "escalation_chain": history}

test_msgs = [
    "What does TCP stand for?",
    "Explain the CAP theorem with a concrete example.",
    "How would you design a globally consistent distributed transaction system?",
]

for msg in test_msgs:
    result = escalating_call(msg)
    escalations = result["escalations"]
    print(f"[{escalations} escalation(s)] Final: {result['final_model'].split('-')[1]:6} | {msg[:55]}")
# Expected Token Savings: 40-65% — easy questions terminate at haiku; hard ones reach opus
# Environment: Q&A agents, customer support bots, mixed-complexity workloads
```

### Option 4: Task-Type Registry with Explicit Model Assignment

Define task types explicitly and assign model tiers at registration time — no ambiguity, full control.

```python
import anthropic
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class TaskType:
    name: str
    model: str
    max_tokens: int
    system_prompt: str
    description: str

client = anthropic.Anthropic()

TASK_REGISTRY: dict[str, TaskType] = {
    # Haiku tasks — simple, fast, cheap
    "classify": TaskType(
        name="classify",
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system_prompt="Classify the input into one of the given categories. Respond with the category name only.",
        description="Single-label classification",
    ),
    "extract_fields": TaskType(
        name="extract_fields",
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system_prompt="Extract the requested fields from the input. Return JSON only.",
        description="Structured field extraction",
    ),
    "translate": TaskType(
        name="translate",
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system_prompt="Translate the input text accurately. Return only the translation.",
        description="Language translation",
    ),

    # Sonnet tasks — moderate complexity
    "write_code": TaskType(
        name="write_code",
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system_prompt="You are an expert Python engineer. Write clean, typed, well-commented code.",
        description="Code generation",
    ),
    "summarize": TaskType(
        name="summarize",
        model="claude-sonnet-4-6",
        max_tokens=512,
        system_prompt="Summarize the input concisely, preserving all key information.",
        description="Summarization",
    ),
    "debug": TaskType(
        name="debug",
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system_prompt="You are an expert debugger. Identify the root cause and provide a fix.",
        description="Bug diagnosis and fixing",
    ),

    # Opus tasks — high complexity
    "architect": TaskType(
        name="architect",
        model="claude-opus-4-6",
        max_tokens=4096,
        system_prompt="You are a principal software architect. Provide comprehensive, well-reasoned architectural guidance.",
        description="System architecture design",
    ),
    "security_review": TaskType(
        name="security_review",
        model="claude-opus-4-6",
        max_tokens=3000,
        system_prompt="You are a senior security engineer. Perform a thorough security analysis.",
        description="Security vulnerability analysis",
    ),
}

def execute_task(task_name: str, user_input: str, extra_context: str = "") -> dict:
    if task_name not in TASK_REGISTRY:
        raise ValueError(f"Unknown task type: {task_name}. Available: {list(TASK_REGISTRY.keys())}")

    task = TASK_REGISTRY[task_name]
    message = f"{extra_context}\n\n{user_input}".strip() if extra_context else user_input

    response = client.messages.create(
        model=task.model,
        max_tokens=task.max_tokens,
        system=task.system_prompt,
        messages=[{"role": "user", "content": message}],
    )

    return {
        "task": task_name,
        "model": task.model,
        "result": response.content[0].text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }

# Demo: different tasks routed to different models
examples = [
    ("classify", "Is this feedback positive or negative? 'The product works great!'", ""),
    ("write_code", "Write a Python function to validate email addresses.", ""),
    ("architect", "Design a real-time notification system for 10M users.", ""),
]

for task_name, user_input, context in examples:
    result = execute_task(task_name, user_input, context)
    tier = "haiku" if "haiku" in result["model"] else ("sonnet" if "sonnet" in result["model"] else "opus")
    print(f"[{task_name:15}] → {tier:6} | in={result['input_tokens']:4} out={result['output_tokens']:4}")
# Expected Token Savings: 50-70% — explicit registry eliminates wrong-model calls entirely
# Environment: Multi-purpose agents, pipeline orchestrators, agent frameworks
```

### Option 5: Cost-Aware Request Queue with Budget Enforcement

Queue requests with cost estimates. Enforce a per-minute and per-user budget cap that automatically routes overflow to cheaper models.

```python
import anthropic
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CostBudget:
    per_minute_usd: float = 0.10
    per_user_per_day_usd: float = 1.00
    _user_spend: dict = field(default_factory=lambda: defaultdict(float))
    _minute_window: deque = field(default_factory=lambda: deque())
    _minute_spend: float = 0.0

    def record_spend(self, user_id: str, cost: float):
        now = time.time()
        self._user_spend[user_id] += cost
        self._minute_window.append((now, cost))
        self._minute_spend += cost
        # Evict entries older than 60s
        while self._minute_window and now - self._minute_window[0][0] > 60:
            _, old_cost = self._minute_window.popleft()
            self._minute_spend -= old_cost

    def can_afford(self, user_id: str, estimated_cost: float) -> tuple[bool, str]:
        if self._minute_spend + estimated_cost > self.per_minute_usd:
            return False, "per-minute budget exceeded"
        if self._user_spend[user_id] + estimated_cost > self.per_user_per_day_usd:
            return False, f"user {user_id} daily budget exceeded"
        return True, ""

INPUT_PRICE_PER_M = {
    "claude-haiku-4-5-20251001": 0.80,
    "claude-sonnet-4-6":         3.00,
    "claude-opus-4-6":          15.00,
}
OUTPUT_PRICE_PER_M = {
    "claude-haiku-4-5-20251001":  4.00,
    "claude-sonnet-4-6":         15.00,
    "claude-opus-4-6":           75.00,
}

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * INPUT_PRICE_PER_M[model]
        + output_tokens / 1_000_000 * OUTPUT_PRICE_PER_M[model]
    )

PREFERRED_MODEL = "claude-sonnet-4-6"
FALLBACK_MODEL  = "claude-haiku-4-5-20251001"

budget = CostBudget(per_minute_usd=0.05, per_user_per_day_usd=0.50)

def budget_aware_call(user_id: str, user_message: str) -> dict:
    # Estimate cost for preferred model
    est_input = len(user_message.split()) * 1.3  # rough token estimate
    est_output = 300
    preferred_cost = estimate_cost(PREFERRED_MODEL, int(est_input), est_output)
    fallback_cost  = estimate_cost(FALLBACK_MODEL,  int(est_input), est_output)

    # Check if we can afford preferred model
    can_use_preferred, reason = budget.can_afford(user_id, preferred_cost)
    model = PREFERRED_MODEL if can_use_preferred else FALLBACK_MODEL
    cost_estimate = preferred_cost if can_use_preferred else fallback_cost

    if not can_use_preferred:
        can_use_fallback, reason2 = budget.can_afford(user_id, fallback_cost)
        if not can_use_fallback:
            return {"error": f"All models over budget: {reason}", "model": None}

    response = client.messages.create(
        model=model,
        max_tokens=400,
        messages=[{"role": "user", "content": user_message}],
    )

    actual_cost = estimate_cost(
        model,
        response.usage.input_tokens,
        response.usage.output_tokens
    )
    budget.record_spend(user_id, actual_cost)

    return {
        "answer": response.content[0].text,
        "model": model,
        "downgraded": model != PREFERRED_MODEL,
        "downgrade_reason": reason if model != PREFERRED_MODEL else None,
        "actual_cost_usd": round(actual_cost, 6),
    }

# Simulate user burning through budget
for i in range(5):
    result = budget_aware_call("user_alice", f"Question {i}: explain Python concept {i}")
    print(f"Q{i}: model={result.get('model','N/A').split('-')[1] if result.get('model') else 'ERR':6} "
          f"downgraded={result.get('downgraded', False)} "
          f"cost=${result.get('actual_cost_usd', 0):.6f}")
# Expected Token Savings: 40-60% — budget pressure naturally routes to cheaper models
# Environment: Multi-tenant SaaS, freemium agents, cost-capped enterprise deployments
```

### Option 6: Adaptive Router with Performance Feedback

Track success rates per model per task type. Route to the cheapest model that historically succeeds on similar tasks.

```python
import anthropic
import sqlite3
import json
import time
from datetime import datetime

client = anthropic.Anthropic()

class AdaptiveRouter:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT,
                model TEXT,
                success INTEGER,
                latency_ms REAL,
                output_tokens INTEGER,
                timestamp TEXT
            )
        """)
        self.conn.commit()

        # Model tiers in ascending cost order
        self.tiers = [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        ]
        # Minimum success rate to keep using a model for a task type
        self.min_success_rate = 0.80
        self.min_samples = 5

    def get_success_rate(self, task_type: str, model: str) -> tuple[float, int]:
        row = self.conn.execute(
            "SELECT AVG(success), COUNT(*) FROM routing_history WHERE task_type=? AND model=?",
            (task_type, model)
        ).fetchone()
        rate, count = row
        return (float(rate) if rate is not None else 1.0), (count or 0)

    def select_model(self, task_type: str) -> str:
        """Select cheapest model with acceptable success rate."""
        for model in self.tiers:
            rate, count = self.get_success_rate(task_type, model)
            if count < self.min_samples:
                return model  # Not enough data — try cheapest first
            if rate >= self.min_success_rate:
                return model  # This tier is good enough
        return self.tiers[-1]  # Fallback to most capable

    def record_outcome(self, task_type: str, model: str, success: bool,
                       latency_ms: float, output_tokens: int):
        self.conn.execute(
            "INSERT INTO routing_history (task_type, model, success, latency_ms, output_tokens, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_type, model, int(success), latency_ms, output_tokens, datetime.now().isoformat())
        )
        self.conn.commit()

    def route_and_call(self, task_type: str, user_message: str,
                       success_fn=None, max_tokens: int = 512) -> dict:
        model = self.select_model(task_type)
        start = time.monotonic()

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user_message}],
        )

        latency_ms = (time.monotonic() - start) * 1000
        text = response.content[0].text

        # Determine success (use caller's fn or default: non-empty response)
        success = success_fn(text) if success_fn else len(text.strip()) > 10

        self.record_outcome(task_type, model, success, latency_ms, response.usage.output_tokens)

        # If failed, escalate once to next tier
        if not success and model != self.tiers[-1]:
            next_model = self.tiers[self.tiers.index(model) + 1]
            start2 = time.monotonic()
            resp2 = client.messages.create(
                model=next_model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": user_message}]
            )
            latency2 = (time.monotonic() - start2) * 1000
            text = resp2.content[0].text
            success2 = success_fn(text) if success_fn else len(text.strip()) > 10
            self.record_outcome(task_type, next_model, success2, latency2, resp2.usage.output_tokens)
            return {"answer": text, "model": next_model, "escalated": True, "task_type": task_type}

        return {"answer": text, "model": model, "escalated": False, "task_type": task_type}

router = AdaptiveRouter()

# Simulate routing decisions over time
tasks = [
    ("classify", "Is this spam? 'Buy cheap meds now!'", lambda r: any(w in r.lower() for w in ["spam", "yes", "likely"])),
    ("summarize", "Summarize: Python is a high-level programming language known for readability.", None),
    ("classify", "Is this question factual or opinion? 'What is the capital of France?'", None),
]

for task_type, msg, success_fn in tasks:
    result = router.route_and_call(task_type, msg, success_fn)
    tier = result["model"].split("-")[1]
    print(f"[{task_type:10}] → {tier:6} | escalated={result['escalated']} | {msg[:50]}")
# Expected Token Savings: 50-70% over time as router learns which tiers succeed per task type
# Environment: Long-running production agents; pipelines with diverse task mixes
```

## Comparison Table

| Option | Classification Method | Routing Overhead | Accuracy | Best For |
|--------|----------------------|-----------------|----------|----------|
| 1: Heuristic Classifier | Regex + length | None | Medium | Simple pipelines, low-latency requirements |
| 2: LLM Pre-Classifier | Haiku classifies | ~1 cheap API call | High | High-volume, accuracy-critical routing |
| 3: Confidence Escalation | Uncertainty detection | Extra calls on escalation | High | Safety-first — never settle for bad answers |
| 4: Task-Type Registry | Explicit registration | None | Very High | Multi-purpose agents with known task types |
| 5: Budget-Aware Queue | Cost estimation + caps | None | Medium | Multi-tenant SaaS with per-user budgets |
| 6: Adaptive Feedback | Historical success rates | DB write per call | Very High | Long-running agents that learn from outcomes |
