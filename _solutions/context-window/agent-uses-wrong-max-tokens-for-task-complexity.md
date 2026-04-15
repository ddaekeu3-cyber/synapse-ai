---
layout: solution
title: "Agent Uses Wrong max_tokens for Task Complexity"
category: context-window
description: "A fixed max_tokens=512 truncates long code generation tasks. A fixed max_tokens=4096 wastes money on one-word answers. Neither adapts to what the task actually requires."
tags: [max-tokens, token-budget, task-complexity, cost-optimization, truncation, adaptive]
---

## Symptom

Two problems from the same root: (1) the agent truncates a 200-line function mid-generation because `max_tokens=256`, leaving broken code; (2) the agent answers "yes" with `max_tokens=4096` — you paid for 4000 potential tokens on a binary response. Both indicate a hardcoded `max_tokens` that ignores what the task actually needs.

## Root Cause

`max_tokens` is set once at agent initialization and applied identically to every call. Short Q&A, long code generation, document summarization, and one-word confirmations all use the same budget. This creates a trilemma: set it too low (truncation), too high (wasted cost), or accept that some tasks will be wrong.

## Fix

---

### Option 1: Task-Type Router with Pre-Defined Token Budgets

Classify the request type and select an appropriate `max_tokens` budget.

```python
import re
import anthropic

client = anthropic.Anthropic()

# Token budgets by task type
TASK_BUDGETS = {
    "yes_no":          32,    # Binary answers
    "short_answer":    128,   # One-paragraph answers
    "explanation":     512,   # Conceptual explanations
    "list":            512,   # Bullet point lists
    "code_snippet":    1024,  # Small functions, scripts
    "code_file":       4096,  # Full file generation
    "document":        8192,  # Reports, essays, docs
    "analysis":        2048,  # Detailed analysis
    "translation":     2048,  # Language translation
    "default":         1024,
}

# Patterns for task detection (order matters — more specific first)
TASK_PATTERNS = [
    ("yes_no",       r"\b(yes or no|true or false|is it|does it|can you confirm|are you)\b"),
    ("code_file",    r"\b(full file|entire (file|module|class|application)|complete implementation|write a (full|complete))\b"),
    ("code_snippet", r"\b(function|method|class|snippet|example|script|implement|write (a|the|some) code)\b"),
    ("document",     r"\b(essay|report|article|document|blog post|write (a|an|the) \d{3,})\b"),
    ("analysis",     r"\b(analyze|analyse|explain in detail|deep dive|comprehensive|thorough)\b"),
    ("translation",  r"\b(translate|in (spanish|french|german|japanese|chinese|korean))\b"),
    ("list",         r"\b(list|enumerate|give me \d+|top \d+|bullet points?)\b"),
    ("explanation",  r"\b(what is|how does|explain|describe|tell me about)\b"),
]


def classify_task(user_message: str) -> tuple[str, int]:
    """Classify message and return (task_type, max_tokens)."""
    msg_lower = user_message.lower()

    for task_type, pattern in TASK_PATTERNS:
        if re.search(pattern, msg_lower, re.IGNORECASE):
            return task_type, TASK_BUDGETS[task_type]

    return "default", TASK_BUDGETS["default"]


def adaptive_chat(user_message: str, system: str = "You are a helpful assistant.") -> tuple[str, str, int]:
    """
    Chat with automatically selected max_tokens.
    Returns (response, task_type, max_tokens_used).
    """
    task_type, max_tokens = classify_task(user_message)
    print(f"  [Task: {task_type}, max_tokens: {max_tokens}]")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    actual_output = response.usage.output_tokens
    efficiency = actual_output / max_tokens * 100
    print(f"  [Used {actual_output}/{max_tokens} tokens ({efficiency:.0f}% utilization)]")

    return response.content[0].text, task_type, max_tokens


# Test with varied tasks
test_cases = [
    "Is Python a dynamically typed language?",
    "List the top 5 Python web frameworks.",
    "Write a Python function to sort a list of dicts by a key.",
    "Explain how Python's GIL works.",
    "Write a complete Python module for a REST API client with authentication, retry logic, and rate limiting.",
]

for msg in test_cases:
    print(f"\nQ: {msg[:70]}")
    reply, task, tokens = adaptive_chat(msg)
    print(f"A: {reply[:100]}...")
```

**Expected Token Savings**: Short Q&A at max_tokens=32 vs 4096 saves ~99% of potential cost for those calls. Across mixed workloads, expect 40–70% savings vs a uniform high budget.
**Environment**: Pure Python, no dependencies. Tune `TASK_BUDGETS` based on your workload distribution.

---

### Option 2: Input-Length-Based Budget Estimation

Estimate required output tokens from the input length and task characteristics.

```python
import anthropic

client = anthropic.Anthropic()

# Typical output:input ratios by task type
OUTPUT_INPUT_RATIOS = {
    "summarization":  0.25,  # Output is ~25% of input length
    "translation":    1.10,  # Output ≈ input (slight expansion)
    "code_from_spec": 5.0,   # Spec → code: 5× expansion typical
    "qa":             0.5,   # Answer ≈ half the question length
    "expansion":      3.0,   # Expand brief to full: 3× expansion
}

MIN_TOKENS = 64
MAX_TOKENS = 8192


def estimate_max_tokens(
    user_message: str,
    task_type: str = "qa",
    context_documents: list[str] | None = None,
    safety_multiplier: float = 1.3,
) -> int:
    """
    Estimate max_tokens based on input length and task type.
    Adds a safety margin to avoid truncation.
    """
    # Estimate input tokens (rough: 4 chars ≈ 1 token)
    message_tokens = len(user_message) // 4
    doc_tokens = sum(len(doc) // 4 for doc in (context_documents or []))
    total_input_tokens = message_tokens + doc_tokens

    ratio = OUTPUT_INPUT_RATIOS.get(task_type, 1.0)
    estimated_output = int(total_input_tokens * ratio * safety_multiplier)

    # Clamp to bounds
    return max(MIN_TOKENS, min(MAX_TOKENS, estimated_output))


def smart_create(
    user_message: str,
    task_type: str = "qa",
    context_documents: list[str] | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> anthropic.types.Message:
    """Create with estimated max_tokens based on task and input size."""
    max_tokens = estimate_max_tokens(user_message, task_type, context_documents)
    print(f"  [Estimated max_tokens: {max_tokens} for task_type={task_type}]")

    messages = [{"role": "user", "content": user_message}]
    if context_documents:
        context = "\n\n".join(f"Document {i+1}:\n{doc}" for i, doc in enumerate(context_documents))
        messages = [{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {user_message}"}]

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )

    actual = response.usage.output_tokens
    if actual >= max_tokens * 0.95:
        print(f"  ⚠️ Response near token limit ({actual}/{max_tokens}). Consider increasing budget.")
    else:
        print(f"  ✓ Used {actual}/{max_tokens} tokens ({actual/max_tokens*100:.0f}%)")

    return response


# Summarization: short output
long_doc = "Machine learning is a subset of artificial intelligence... " * 50
r = smart_create(
    "Summarize the key points of this document.",
    task_type="summarization",
    context_documents=[long_doc],
)
print(r.content[0].text[:100])

# Code generation from spec: needs more tokens
spec = "Function that: validates email, checks domain DNS, rate-limits to 100/hour, logs attempts"
r = smart_create(spec, task_type="code_from_spec")
print(r.content[0].text[:200])
```

**Expected Token Savings**: Input-proportional estimation prevents systematic over-allocation. Summarization of a 2000-token doc uses max_tokens=650 instead of 4096 — 84% savings.
**Environment**: No dependencies. Calibrate ratios from your actual workload samples.

---

### Option 3: Streaming + Early Stop on Completion Detection

Stream the response. Stop when the output is semantically complete, rather than waiting for `max_tokens`.

```python
import anthropic
import re

client = anthropic.Anthropic()

# Signals that indicate a complete response
COMPLETION_SIGNALS = [
    r"\n```\s*$",              # Closed code block
    r"\n\d+\.\s+[A-Z].*\.\s*$",  # Numbered list end
    r"(In summary|To summarize|In conclusion)[^.]*\.\s*$",  # Conclusion
    r"Hope this helps!?\s*$",  # Conversational end
    r"\n---\s*$",             # Horizontal rule
]

MAX_TOKENS_HARD_LIMIT = 4096  # Never go above this
EARLY_STOP_CHECK_EVERY = 50   # Check completion every N tokens


def streaming_with_smart_stop(
    user_message: str,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, int]:
    """
    Stream with semantic completion detection.
    Returns (full_text, tokens_used).
    """
    collected = []
    total_tokens = 0
    stopped_early = False

    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS_HARD_LIMIT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for token in stream.text_stream:
            collected.append(token)
            total_tokens += 1  # rough token count

            # Periodically check for completion signals
            if total_tokens % EARLY_STOP_CHECK_EVERY == 0:
                current_text = "".join(collected)
                for pattern in COMPLETION_SIGNALS:
                    if re.search(pattern, current_text, re.MULTILINE):
                        stopped_early = True
                        print(f"  [Early stop at ~{total_tokens} tokens] Completion signal detected")
                        # Note: can't actually stop the Anthropic stream mid-response
                        # but we can stop processing. The SDK will continue until stop_reason.
                        break

    full_text = "".join(collected)
    actual_tokens = stream.get_final_message().usage.output_tokens if not stopped_early else total_tokens
    return full_text, actual_tokens


# Dynamic max_tokens based on question complexity heuristic
def heuristic_max_tokens(message: str) -> int:
    words = len(message.split())
    has_code_request = any(kw in message.lower() for kw in ["write", "implement", "code", "function", "class"])
    has_list_request = any(kw in message.lower() for kw in ["list", "enumerate", "give me"])
    has_detail_request = any(kw in message.lower() for kw in ["detail", "explain", "comprehensive"])

    base = 256
    if has_code_request:   base = 2048
    if has_list_request:   base = max(base, 512)
    if has_detail_request: base = max(base, 1024)

    # Scale slightly with question length
    base = int(base * (1 + words / 200))
    return min(base, MAX_TOKENS_HARD_LIMIT)


def smart_stream(message: str) -> str:
    max_t = heuristic_max_tokens(message)
    print(f"  [Heuristic max_tokens: {max_t}]")

    collected = []
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_t,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        for token in stream.text_stream:
            collected.append(token)
            print(token, end="", flush=True)
    print()

    final = stream.get_final_message()
    used = final.usage.output_tokens
    print(f"  [Used {used}/{max_t} tokens]")
    return "".join(collected)


for msg in [
    "Is Redis faster than PostgreSQL for caching? Yes or no.",
    "List 5 Python testing frameworks.",
    "Write a Python class for a thread-safe LRU cache.",
]:
    print(f"\nQ: {msg}")
    smart_stream(msg)
```

**Expected Token Savings**: Heuristic sizing prevents 4096-token budgets on yes/no questions. Streaming allows accurate utilization measurement for tuning.
**Environment**: Streaming SDK. Cannot truly stop mid-stream on Anthropic's API — but heuristic sizing achieves the same cost reduction.

---

### Option 4: Tiered Model + Token Budget Co-Selection

Select both the model AND the token budget together based on task complexity. Simple tasks get Haiku + small budget; complex tasks get Sonnet + large budget.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class TaskProfile:
    task_type: str
    model: str
    max_tokens: int
    description: str


TASK_PROFILES = {
    "trivial": TaskProfile(
        task_type="trivial",
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        description="Yes/no, simple lookups, date conversions",
    ),
    "simple": TaskProfile(
        task_type="simple",
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        description="Short explanations, simple Q&A",
    ),
    "moderate": TaskProfile(
        task_type="moderate",
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        description="Code snippets, lists, explanations",
    ),
    "complex": TaskProfile(
        task_type="complex",
        model="claude-sonnet-4-6",
        max_tokens=4096,
        description="Multi-step reasoning, full code files, analysis",
    ),
    "intensive": TaskProfile(
        task_type="intensive",
        model="claude-sonnet-4-6",
        max_tokens=8192,
        description="Long documents, complex architecture, research",
    ),
}

PROFILE_SELECTOR_TOOL = {
    "name": "select_task_profile",
    "description": "Select the appropriate task profile for this request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "enum": list(TASK_PROFILES.keys()),
                "description": "Task complexity profile",
            },
            "reasoning": {"type": "string"},
        },
        "required": ["profile", "reasoning"],
    },
}

# Use tiny Haiku call to classify before the main call
_classifier = anthropic.Anthropic()


def select_profile(user_message: str) -> TaskProfile:
    """Use a cheap Haiku call to classify the task and select the right profile."""
    response = _classifier.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=[PROFILE_SELECTOR_TOOL],
        tool_choice={"type": "any"},
        system=(
            "Classify the complexity of this request and select a task profile.\n"
            "trivial: yes/no questions\n"
            "simple: short factual answers\n"
            "moderate: paragraphs, short code\n"
            "complex: multi-file code, detailed analysis\n"
            "intensive: long documents, comprehensive research"
        ),
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "select_task_profile":
            profile_name = block.input["profile"]
            return TASK_PROFILES[profile_name]

    return TASK_PROFILES["moderate"]  # safe default


def tiered_create(user_message: str, system: str = "You are a helpful assistant.") -> str:
    """Select model + max_tokens based on task profile, then execute."""
    profile = select_profile(user_message)
    print(f"  [Profile: {profile.task_type}] model={profile.model}, max_tokens={profile.max_tokens}")
    print(f"  [Description: {profile.description}]")

    response = client.messages.create(
        model=profile.model,
        max_tokens=profile.max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    actual = response.usage.output_tokens
    print(f"  [Used {actual}/{profile.max_tokens} tokens]")
    return response.content[0].text


# Test
requests = [
    "Is 17 a prime number?",
    "What is the difference between TCP and UDP?",
    "Write a Python class implementing a binary search tree with insert, search, and delete.",
    "Design a complete microservices architecture for an e-commerce platform with 50,000 daily active users.",
]

for req in requests:
    print(f"\nRequest: {req[:80]}")
    reply = tiered_create(req)
    print(f"Reply: {reply[:150]}...")
```

**Expected Token Savings**: Trivial tasks at Haiku+64 tokens cost ~$0.000001. Same task at Opus+4096 costs ~$0.006. For 1000 trivial queries/day: ~$6/day saved.
**Environment**: Two API calls per request (classifier + main). Classification costs ~20 Haiku tokens — pays back immediately on any non-trivial routing.

---

### Option 5: Adaptive Budget with Utilization Feedback Loop

Track utilization per task type. Automatically adjust budgets based on actual usage patterns.

```python
import json
import sqlite3
import time
import anthropic

client = anthropic.Anthropic()

# Persistent utilization tracking
perf_conn = sqlite3.connect("token_budgets.db")
perf_conn.execute("""
    CREATE TABLE IF NOT EXISTS utilization (
        task_type  TEXT,
        budget     INTEGER,
        used       INTEGER,
        ts         REAL
    )
""")
perf_conn.commit()

# Starting budgets
BUDGETS = {
    "qa":       512,
    "code":     2048,
    "doc":      4096,
    "summary":  1024,
}
LEARN_RATE = 0.1          # How fast to adjust (0=no adjust, 1=full replace)
MIN_BUDGET = 64
MAX_BUDGET = 8192
UTILIZATION_TARGET = 0.75  # Target 75% utilization


def record_utilization(task_type: str, budget: int, used: int):
    perf_conn.execute(
        "INSERT INTO utilization VALUES (?,?,?,?)",
        (task_type, budget, used, time.time())
    )
    perf_conn.commit()


def get_recommended_budget(task_type: str) -> int:
    """
    Get budget adjusted from recent utilization data.
    If p90 usage is below 60% of budget → shrink.
    If p90 usage exceeds 90% of budget → grow.
    """
    rows = perf_conn.execute(
        "SELECT used, budget FROM utilization WHERE task_type=? ORDER BY ts DESC LIMIT 50",
        (task_type,),
    ).fetchall()

    if len(rows) < 5:
        return BUDGETS.get(task_type, 1024)  # Not enough data yet

    usages = sorted([r[0] for r in rows])
    p90_used = usages[int(len(usages) * 0.9)]
    current_budget = BUDGETS.get(task_type, 1024)

    util_rate = p90_used / current_budget
    if util_rate < 0.6:
        # Shrink budget
        new_budget = int(current_budget * (1 - LEARN_RATE * (0.6 - util_rate)))
    elif util_rate > 0.9:
        # Grow budget (add 20% headroom above p90)
        new_budget = int(p90_used * 1.2)
    else:
        new_budget = current_budget

    new_budget = max(MIN_BUDGET, min(MAX_BUDGET, new_budget))
    if new_budget != current_budget:
        BUDGETS[task_type] = new_budget
        print(f"  [Budget updated] {task_type}: {current_budget} → {new_budget} (p90={p90_used})")

    return new_budget


def adaptive_create(
    user_message: str,
    task_type: str = "qa",
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    max_tokens = get_recommended_budget(task_type)
    print(f"  [Adaptive budget] {task_type}: max_tokens={max_tokens}")

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )

    used = response.usage.output_tokens
    record_utilization(task_type, max_tokens, used)
    print(f"  [Utilization] {used}/{max_tokens} ({used/max_tokens*100:.0f}%)")

    if response.stop_reason == "max_tokens":
        print(f"  ⚠️ TRUNCATED — budget too small for this response")

    return response.content[0].text


# Simulate usage over time — budgets auto-adjust
qa_questions = [
    "What is Python?",
    "Name three sorting algorithms.",
    "What is a REST API?",
    "Define machine learning.",
    "What does CPU stand for?",
]
for q in qa_questions[:3]:
    print(f"\nQ: {q}")
    adaptive_create(q, "qa")

# After a few uses, budget recommendation is calibrated
print(f"\n[Final QA budget recommendation: {get_recommended_budget('qa')}]")
```

**Expected Token Savings**: Self-calibrating budgets converge to actual usage. Over time, over-budgeted task types shrink automatically without manual tuning.
**Environment**: SQLite utilization log. Resets BUDGETS dict in memory — persist to DB for production.

---

### Option 6: Max-Tokens Negotiation via Structured Request

Let the model declare how many tokens it estimates it needs before generating.

```python
import anthropic

client = anthropic.Anthropic()

NEGOTIATE_TOOL = {
    "name": "declare_token_estimate",
    "description": "Estimate how many output tokens you'll need to fully answer this request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "estimated_tokens": {
                "type": "integer",
                "description": "Realistic token count needed for a complete answer",
            },
            "response_type": {
                "type": "string",
                "enum": ["one_word", "sentence", "paragraph", "list", "code", "long_form"],
            },
            "can_be_shorter": {
                "type": "boolean",
                "description": "True if you could give a useful but shorter answer if needed",
            },
        },
        "required": ["estimated_tokens", "response_type", "can_be_shorter"],
    },
}

CEILING = 8192  # Hard maximum we'll ever allow
FLOOR = 32


def negotiated_create(
    user_message: str,
    model: str = "claude-haiku-4-5-20251001",
    user_max: int | None = None,
) -> str:
    """
    Phase 1: Model estimates token needs (cheap call).
    Phase 2: Generate with the negotiated budget.
    """
    # Phase 1: Negotiate
    negotiation = client.messages.create(
        model=model,
        max_tokens=128,  # Only need a small response for the estimate
        tools=[NEGOTIATE_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": (
                f"Before answering, estimate how many tokens you'll need.\n\n"
                f"Request: {user_message}"
            ),
        }],
    )

    estimate = 1024  # default
    for block in negotiation.content:
        if block.type == "tool_use" and block.name == "declare_token_estimate":
            estimate = block.input["estimated_tokens"]
            resp_type = block.input["response_type"]
            can_shorten = block.input["can_be_shorter"]
            print(f"  [Negotiated] estimate={estimate}, type={resp_type}, can_shorten={can_shorten}")

    # Apply constraints
    actual_max = max(FLOOR, min(CEILING, estimate))
    if user_max:
        actual_max = min(actual_max, user_max)
        if estimate > user_max:
            print(f"  [Budget constrained] Model wanted {estimate}, user cap is {user_max}")

    print(f"  [Final max_tokens: {actual_max}]")

    # Phase 2: Generate with negotiated budget
    response = client.messages.create(
        model=model,
        max_tokens=actual_max,
        messages=[{"role": "user", "content": user_message}],
    )

    actual_used = response.usage.output_tokens
    print(f"  [Used {actual_used}/{actual_max} tokens]")
    return response.content[0].text


# Test
for msg in [
    "What year was Python created?",
    "Write a complete Python implementation of a B-tree data structure.",
    "Give me a bullet list of 5 REST API best practices.",
]:
    print(f"\nQ: {msg[:80]}")
    reply = negotiated_create(msg)
    print(f"A: {reply[:150]}...")
```

**Expected Token Savings**: Model self-estimates 45 tokens for a year answer vs your hardcoded 512. Budget negotiation reduces average over-allocation by 30–60% across mixed workloads.
**Environment**: Two API calls (negotiate + generate). Total overhead: ~30 Haiku tokens for estimation. Best for high-volume, mixed-complexity workloads.

---

| Option | Approach | Latency Added | Self-Adjusting | Best For |
|--------|---------|--------------|---------------|---------|
| 1 | Task-type routing table | None | No | Predictable task types |
| 2 | Input-length ratio | None | No | Proportional tasks (summarization) |
| 3 | Streaming + heuristic | None | No | Real-time feedback on utilization |
| 4 | Tiered model + budget | +1 classifier call | No | Cost-quality co-optimization |
| 5 | Utilization feedback loop | None | Yes | Production with historical data |
| 6 | Model self-estimation | +1 negotiate call | No | Unknown/mixed task distributions |
