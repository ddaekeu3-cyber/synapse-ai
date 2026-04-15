---
layout: solution
title: "Agent Uses Expensive Model for Simple Routing Decisions"
category: token-cost
description: "Every request — including trivial classification, intent detection, and routing decisions — goes through the most powerful (and expensive) model. A question like 'Is this a billing question or a technical question?' costs the same as a complex code review. Simple classification tasks that Haiku handles correctly at 1/75th the cost are routed to Opus. The fix is a tiered model routing strategy that matches model capability to task complexity."
tags: [token-cost, model-routing, haiku, cost-optimization, classification, routing, tiered-models]
---

# Agent Uses Expensive Model for Simple Routing Decisions

## Symptom
- Every API call uses `claude-opus-4-6` regardless of task complexity
- Simple yes/no classification questions cost the same as complex generation tasks
- Intent routing, language detection, and topic classification burn expensive model tokens
- Monthly bill is 10–50× higher than necessary for equivalent output quality
- Latency is high even for simple queries because the expensive model is always used
- Haiku would get 95%+ of classifications right but is never used

## Root Cause
The agent was built with a single model configured globally. When new capabilities were added, they all inherited the same expensive model. Simple operations like classification, routing, and validation don't need the full capability of Opus or Sonnet — they need fast, cheap, accurate answers to constrained questions. Matching model to task complexity is the highest-leverage cost optimization available.

## Fix

### Option 1: Tiered router — Haiku for classification, Sonnet/Opus for generation

```python
import anthropic
import json

client = anthropic.Anthropic()

# Model cost reference (approximate, as of 2025):
# claude-haiku-4-5-20251001:  $0.80/$4.00 per MTok in/out  (1×)
# claude-sonnet-4-6:          $3/$15 per MTok               (3.75×/3.75×)
# claude-opus-4-6:            $15/$75 per MTok              (18.75×/18.75×)

MODELS = {
    "classify": "claude-haiku-4-5-20251001",   # fast, cheap, accurate for classification
    "generate": "claude-sonnet-4-6",           # balanced for standard generation
    "reason": "claude-opus-4-6",              # complex multi-step reasoning only
}


def classify_request(user_message: str) -> dict:
    """
    Use Haiku to classify the request type.
    Cost: ~$0.001 (vs ~$0.015 for Sonnet, ~$0.075 for Opus)
    """
    response = client.messages.create(
        model=MODELS["classify"],
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"""Classify this message into exactly one category.
Reply with JSON only: {{"category": "<category>", "complexity": "<simple|medium|complex>"}}

Categories: billing, technical_support, general_question, complaint, feature_request, other

Message: {user_message}"""
        }]
    )
    text = response.content[0].text.strip()
    import re
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"category": "other", "complexity": "medium"}


def route_to_model(classification: dict) -> str:
    """Select the cheapest model that can handle the task."""
    complexity = classification.get("complexity", "medium")
    category = classification.get("category", "other")

    # Use Opus only for genuinely complex tasks:
    if complexity == "complex" and category in ["technical_support", "feature_request"]:
        return MODELS["reason"]

    # Use Sonnet for medium-complexity generation:
    if complexity in ["medium", "complex"]:
        return MODELS["generate"]

    # Use Haiku for simple responses:
    return MODELS["classify"]


def smart_agent(user_message: str) -> dict:
    """
    Route each request to the cheapest model that can handle it.
    """
    # Step 1: classify (always Haiku — cheap)
    classification = classify_request(user_message)
    model = route_to_model(classification)

    print(f"[routing] category={classification['category']}, "
          f"complexity={classification['complexity']}, model={model.split('-')[1]}")

    # Step 2: generate response (model selected by complexity)
    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}]
    )

    return {
        "response": response.content[0].text,
        "model_used": model,
        "classification": classification,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens
    }


# Test:
queries = [
    "What's my account balance?",                          # billing → Haiku
    "How do I reset my password?",                         # simple technical → Haiku
    "Write a Python script to parse XML files",             # medium technical → Sonnet
    "Design a distributed caching strategy for 10M users", # complex → Opus
]
for q in queries:
    result = smart_agent(q)
    print(f"  Query: {q[:50]}")
    print(f"  Model: {result['model_used'].split('-')[1]}")
    print()
```

### Option 2: Task-specific model assignment — per-tool model selection

```python
import anthropic
import json

client = anthropic.Anthropic()

# Assign a model to each tool based on what that tool actually needs.
# Simple tools (format check, language detect) use Haiku.
# Complex tools (code generation, analysis) use Sonnet or Opus.

TOOL_MODELS = {
    # Haiku: classification, validation, simple extraction
    "classify_intent": "claude-haiku-4-5-20251001",
    "detect_language": "claude-haiku-4-5-20251001",
    "extract_entities": "claude-haiku-4-5-20251001",
    "validate_format": "claude-haiku-4-5-20251001",
    "check_eligibility": "claude-haiku-4-5-20251001",
    "summarize_short": "claude-haiku-4-5-20251001",

    # Sonnet: generation, explanation, moderate complexity
    "generate_response": "claude-sonnet-4-6",
    "summarize_long": "claude-sonnet-4-6",
    "write_email": "claude-sonnet-4-6",
    "explain_concept": "claude-sonnet-4-6",

    # Opus: complex reasoning, code architecture, analysis
    "debug_code": "claude-opus-4-6",
    "design_system": "claude-opus-4-6",
    "legal_analysis": "claude-opus-4-6",
    "strategic_planning": "claude-opus-4-6",
}


def call_with_appropriate_model(
    tool_name: str,
    prompt: str,
    max_tokens: int = 256
) -> dict:
    """Call the appropriate model for a given tool."""
    model = TOOL_MODELS.get(tool_name, "claude-sonnet-4-6")

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )

    cost_tier = {
        "claude-haiku-4-5-20251001": "low",
        "claude-sonnet-4-6": "medium",
        "claude-opus-4-6": "high"
    }.get(model, "medium")

    return {
        "result": response.content[0].text,
        "model": model,
        "cost_tier": cost_tier,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens
    }


# Multi-step pipeline with per-step model selection:
def process_support_ticket(ticket_text: str) -> dict:
    """
    Process a support ticket using the cheapest appropriate model at each step.
    """
    # Step 1: Classify (Haiku)
    classification = call_with_appropriate_model(
        "classify_intent",
        f"Classify this support ticket in one word (billing/technical/account/other): {ticket_text}",
        max_tokens=16
    )

    # Step 2: Extract entities (Haiku)
    entities = call_with_appropriate_model(
        "extract_entities",
        f"Extract: account_id, product, issue_type from: {ticket_text}. Reply JSON only.",
        max_tokens=128
    )

    # Step 3: Generate response (Sonnet — needs quality response)
    intent = classification["result"].strip().lower()
    response = call_with_appropriate_model(
        "generate_response",
        f"Write a helpful support response for this {intent} ticket:\n{ticket_text}",
        max_tokens=512
    )

    total_input_tokens = sum(r["input_tokens"] for r in [classification, entities, response])
    total_output_tokens = sum(r["output_tokens"] for r in [classification, entities, response])

    return {
        "classification": classification["result"],
        "entities": entities["result"],
        "response": response["result"],
        "models_used": {
            "classification": classification["model"].split("-")[1],
            "entities": entities["model"].split("-")[1],
            "response": response["model"].split("-")[1]
        },
        "tokens": {"input": total_input_tokens, "output": total_output_tokens}
    }
```

### Option 3: Complexity scorer — dynamic model selection based on prompt analysis

```python
import anthropic
import re

client = anthropic.Anthropic()

def score_complexity(message: str) -> float:
    """
    Score prompt complexity 0.0 (trivial) to 1.0 (very complex).
    Used to select the appropriate model tier.
    No LLM call needed — pure heuristics.
    """
    score = 0.0
    message_lower = message.lower()

    # Length signal:
    words = len(message.split())
    if words > 200: score += 0.3
    elif words > 100: score += 0.2
    elif words > 50: score += 0.1

    # Task complexity signals:
    complex_keywords = [
        "design", "architecture", "optimize", "analyze", "compare",
        "strategy", "tradeoffs", "implement", "refactor", "debug",
        "explain why", "how does", "what causes"
    ]
    simple_keywords = [
        "is this", "what is", "yes or no", "classify", "translate",
        "format", "convert", "extract", "find the", "list the"
    ]

    score += 0.1 * sum(1 for kw in complex_keywords if kw in message_lower)
    score -= 0.05 * sum(1 for kw in simple_keywords if kw in message_lower)

    # Code signals:
    if re.search(r'```|def |class |import |function', message):
        score += 0.2

    # Multi-step signals:
    if re.search(r'(first|then|finally|step \d|and also|additionally)', message_lower):
        score += 0.15

    # Constrained output signals (simpler):
    if re.search(r'(json|yes.or.no|one word|single|true.or.false|classify)', message_lower):
        score -= 0.2

    return max(0.0, min(1.0, score))


def select_model(complexity_score: float) -> str:
    """Select model based on complexity score."""
    if complexity_score < 0.2:
        return "claude-haiku-4-5-20251001"   # trivial
    elif complexity_score < 0.6:
        return "claude-sonnet-4-6"           # moderate
    else:
        return "claude-opus-4-6"             # complex


def complexity_routed_agent(user_message: str) -> dict:
    complexity = score_complexity(user_message)
    model = select_model(complexity)

    print(f"[complexity-router] score={complexity:.2f}, model={model.split('-')[1]}")

    response = client.messages.create(
        model=model,
        max_tokens=min(512, max(64, int(complexity * 1024))),
        messages=[{"role": "user", "content": user_message}]
    )

    return {
        "response": response.content[0].text,
        "complexity_score": complexity,
        "model_used": model.split("-")[1]
    }


# Tests:
examples = [
    "Is 'hello@email.com' a valid email address?",
    "Translate 'good morning' to Spanish",
    "Write a Python function to parse CSV files",
    "Design a microservices architecture for a global e-commerce platform with 100M users",
]
for msg in examples:
    r = complexity_routed_agent(msg)
    print(f"  [{r['complexity_score']:.2f}] → {r['model_used']}: {msg[:50]}")
```

### Option 4: Cascading fallback — try cheaper model first, escalate on low confidence

```python
import anthropic
import re

client = anthropic.Anthropic()

MODELS_IN_ORDER = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6"
]


def extract_confidence(response_text: str) -> float:
    """
    Extract confidence score from response if available.
    Expects the model to end with "CONFIDENCE: 0.X"
    """
    match = re.search(r'confidence:\s*(0?\.\d+|\d+(?:\.\d+)?)', response_text, re.IGNORECASE)
    if match:
        return min(1.0, float(match.group(1)))
    # If no confidence marker, assume high confidence (model answered normally):
    return 0.9


def cascade_response(
    user_message: str,
    confidence_threshold: float = 0.8,
    max_escalations: int = 2
) -> dict:
    """
    Try the cheapest model first. If confidence is low, escalate to the next tier.
    Most requests resolve at Haiku tier; only genuinely hard ones reach Opus.
    """
    system = """Answer the question. At the end of your response, add:
CONFIDENCE: <0.0 to 1.0> (your confidence in the accuracy of your answer)"""

    total_input_tokens = 0
    total_output_tokens = 0
    escalations = 0

    for i, model in enumerate(MODELS_IN_ORDER[:max_escalations + 1]):
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_message}]
        )
        text = response.content[0].text
        confidence = extract_confidence(text)
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        print(f"  [cascade] {model.split('-')[1]}: confidence={confidence:.2f}")

        if confidence >= confidence_threshold or i == len(MODELS_IN_ORDER) - 1:
            # Clean up the confidence marker from the response:
            clean_text = re.sub(r'\nCONFIDENCE:\s*\S+\s*$', '', text).strip()
            return {
                "response": clean_text,
                "model_used": model.split("-")[1],
                "confidence": confidence,
                "escalations": escalations,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens
            }

        escalations += 1
        print(f"  [cascade] Low confidence ({confidence:.2f}), escalating to next tier")

    return {"error": "All models exhausted"}


# Most questions resolve at Haiku:
r1 = cascade_response("What is the capital of France?")   # stays at Haiku
r2 = cascade_response("Prove the Riemann hypothesis")     # escalates quickly
print(f"r1 escalations: {r1['escalations']}, model: {r1['model_used']}")
```

### Option 5: Cost tracking dashboard — measure actual routing efficiency

```python
import anthropic
import time
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.Anthropic()

# Pricing per million tokens (approximate):
COST_PER_M_TOKENS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class CostTracker:
    """Track costs per model to measure routing efficiency."""
    _model_stats: dict = field(default_factory=lambda: defaultdict(lambda: {
        "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0
    }))

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = COST_PER_M_TOKENS.get(model, {"input": 3.0, "output": 15.0})
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
        s = self._model_stats[model]
        s["calls"] += 1
        s["input_tokens"] += input_tokens
        s["output_tokens"] += output_tokens
        s["cost_usd"] += cost
        return cost

    def report(self) -> dict:
        total_cost = sum(s["cost_usd"] for s in self._model_stats.values())
        total_calls = sum(s["calls"] for s in self._model_stats.values())

        # What would it have cost if everything used Opus?
        total_input = sum(s["input_tokens"] for s in self._model_stats.values())
        total_output = sum(s["output_tokens"] for s in self._model_stats.values())
        opus_cost = (total_input * 15 + total_output * 75) / 1_000_000

        return {
            "total_cost_usd": round(total_cost, 4),
            "total_calls": total_calls,
            "savings_vs_opus": round(opus_cost - total_cost, 4),
            "savings_pct": round((1 - total_cost / max(opus_cost, 0.0001)) * 100, 1),
            "per_model": dict(self._model_stats)
        }


tracker = CostTracker()

def tracked_call(model: str, messages: list[dict], max_tokens: int = 256) -> str:
    response = client.messages.create(
        model=model, max_tokens=max_tokens, messages=messages
    )
    cost = tracker.record(model, response.usage.input_tokens, response.usage.output_tokens)
    print(f"  [cost] {model.split('-')[1]}: ${cost:.6f}")
    return response.content[0].text


# After running your agent pipeline, check:
# report = tracker.report()
# print(f"Total cost: ${report['total_cost_usd']}")
# print(f"Savings vs all-Opus: ${report['savings_vs_opus']} ({report['savings_pct']}%)")
```

### Option 6: Static routing table — simple, fast, predictable

```python
import anthropic
import re

client = anthropic.Anthropic()

# For production systems: a static routing table is more predictable
# and auditable than heuristic or LLM-based routing.

ROUTING_TABLE = {
    # Pattern → model
    # Haiku: binary/categorical questions, simple extraction, detection
    r"is\s+(?:this|it|the)\s+\w+": "claude-haiku-4-5-20251001",
    r"(?:classify|categorize|label|tag)\s+": "claude-haiku-4-5-20251001",
    r"(?:detect|identify|find)\s+(?:the\s+)?(?:language|sentiment|intent|tone)": "claude-haiku-4-5-20251001",
    r"(?:translate|convert)\s+(?:this\s+)?(?:to|into)\s+\w+": "claude-haiku-4-5-20251001",
    r"(?:yes|no|true|false|correct|incorrect).*\?$": "claude-haiku-4-5-20251001",
    r"extract\s+(?:the\s+)?(?:name|email|phone|date|number)": "claude-haiku-4-5-20251001",

    # Sonnet: writing, explanation, moderate analysis
    r"(?:write|draft|compose|create)\s+(?:a|an)\s+": "claude-sonnet-4-6",
    r"(?:explain|describe|summarize|outline)\s+": "claude-sonnet-4-6",
    r"(?:compare|contrast)\s+": "claude-sonnet-4-6",
    r"(?:generate|produce)\s+(?:a|an)\s+": "claude-sonnet-4-6",

    # Opus: architecture, complex reasoning, code design
    r"(?:design|architect|plan)\s+(?:a|an|the)\s+(?:system|architecture|strategy|solution)": "claude-opus-4-6",
    r"(?:analyze|debug|optimize)\s+(?:this|the)\s+(?:code|algorithm|performance)": "claude-opus-4-6",
    r"(?:how|why)\s+does\s+.{50,}": "claude-opus-4-6",  # long complex why/how questions
}

DEFAULT_MODEL = "claude-sonnet-4-6"


def route_by_table(user_message: str) -> str:
    """Match message against routing table, return appropriate model."""
    msg_lower = user_message.lower().strip()
    for pattern, model in ROUTING_TABLE.items():
        if re.search(pattern, msg_lower, re.IGNORECASE):
            return model
    return DEFAULT_MODEL


def table_routed_agent(user_message: str) -> dict:
    model = route_by_table(user_message)
    print(f"[table-router] → {model.split('-')[1]}")

    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}]
    )
    return {
        "response": response.content[0].text,
        "model": model.split("-")[1],
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens
    }
```

## Model Selection Decision Matrix

| Task Type | Recommended Model | Reason |
|---|---|---|
| Yes/no classification | Haiku | Binary output, no reasoning needed |
| Intent/topic detection | Haiku | Pattern matching, structured output |
| Language detection | Haiku | Statistical task |
| Entity extraction | Haiku | Constrained extraction |
| Short summarization (<500 words in) | Haiku | Straightforward compression |
| Email/message drafting | Sonnet | Quality matters, moderate complexity |
| Long summarization | Sonnet | Needs coherence across length |
| Code explanation | Sonnet | Requires understanding + clarity |
| Complex code generation | Sonnet/Opus | Depends on complexity |
| System design / architecture | Opus | Deep reasoning, tradeoffs |
| Multi-step analysis | Opus | Chained reasoning needed |
| Legal/medical interpretation | Opus | High accuracy critical |

## Expected Token Savings

| Routing Strategy | Typical Cost Reduction vs All-Opus |
|---|---|
| 50% Haiku / 50% Sonnet | ~80% savings |
| 70% Haiku / 25% Sonnet / 5% Opus | ~92% savings |
| All tasks at Haiku | ~95% savings (quality risk) |
| All tasks at Sonnet | ~80% savings |

A typical multi-step pipeline classifying intent (Haiku) then generating (Sonnet) saves ~83% vs using Opus for both steps.

## Environment
- All production agents with mixed workloads; the tiered router (Option 1) is the highest-leverage single change; start by profiling which tool calls are using expensive models and don't need to — common candidates are classification, routing, validation, and simple extraction; the cost tracker (Option 5) quantifies the savings to justify the routing investment; the static table (Option 6) is fastest to implement and most auditable for compliance-sensitive environments
