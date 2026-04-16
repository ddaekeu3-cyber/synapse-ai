---
layout: solution
title: "Agent Doesn't Implement Token-Budget-Aware Response Formatting"
category: context-window
description: "Agents that ignore remaining context space produce responses that overflow the window, get truncated mid-thought, or cause expensive context resets. These patterns show how to format responses to fit within the available token budget."
tags: [context-window, token-budget, formatting, truncation, context-management, anthropic]
---

## Problem

In long conversations and multi-turn agent loops, the context window fills up. An agent unaware of remaining space will generate verbose responses that overflow the window — causing truncation, forcing expensive context resets, or dropping critical earlier context. Token-budget-aware formatting produces responses sized to fit: brief when space is tight, full when space allows.

---

### Option 1: Remaining-Budget Prompt Injection

Estimate token usage and inject a word-count instruction into each prompt before calling the API.

```python
import anthropic

client = anthropic.Anthropic()
MODEL_CONTEXT_LIMITS = {
    "claude-haiku-4-5-20251001": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
}
TOKENS_PER_WORD = 1.35      # rough approximation
SAFETY_MARGIN = 2000        # reserve for assistant reply overhead

def estimate_tokens(text: str) -> int:
    return int(len(text.split()) * TOKENS_PER_WORD)

def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(block.get("text", ""))
        else:
            total += estimate_tokens(str(content))
    return total + len(messages) * 4   # per-message overhead

def budget_aware_prompt(
    messages: list[dict],
    system: str,
    model: str = "claude-sonnet-4-6",
) -> list[dict]:
    context_limit = MODEL_CONTEXT_LIMITS.get(model, 200_000)
    used = estimate_tokens(system) + estimate_messages_tokens(messages)
    remaining = context_limit - used - SAFETY_MARGIN
    remaining_words = int(remaining / TOKENS_PER_WORD)

    if remaining_words < 100:
        budget_instruction = "\n\n[CRITICAL: Context nearly full. Reply in 1-2 sentences max.]"
    elif remaining_words < 300:
        budget_instruction = f"\n\n[Context tight. Keep reply under {min(200, remaining_words)} words.]"
    elif remaining_words < 800:
        budget_instruction = f"\n\n[Keep reply concise, under {min(500, remaining_words)} words.]"
    else:
        budget_instruction = ""   # no constraint needed

    if budget_instruction:
        patched = messages.copy()
        last = patched[-1].copy()
        last["content"] = str(last.get("content", "")) + budget_instruction
        patched[-1] = last
        return patched, remaining_words
    return messages, remaining_words

def chat(messages: list[dict], system: str = "", model: str = "claude-sonnet-4-6") -> str:
    patched_messages, remaining = budget_aware_prompt(messages, system, model)
    print(f"[estimated remaining tokens: {remaining * TOKENS_PER_WORD:.0f}]")

    max_tokens = min(4096, int(remaining * TOKENS_PER_WORD * 0.8))
    max_tokens = max(max_tokens, 64)

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=patched_messages,
    )
    return response.content[0].text

if __name__ == "__main__":
    history = [
        {"role": "user", "content": "Tell me about distributed systems."},
        {"role": "assistant", "content": "Distributed systems coordinate multiple networked computers..."},
        {"role": "user", "content": "What are the key consistency models?"},
    ]
    reply = chat(history, system="You are a distributed systems expert.")
    print(reply[:400])

# Expected Token Savings: Prevents overflow-induced context resets; saves full conversation re-injection cost
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Tiered Format Selection Based on Budget

Pick a response format (full / summary / bullet / one-liner) based on available token budget.

```python
import anthropic
from enum import Enum

client = anthropic.Anthropic()

class ResponseFormat(Enum):
    FULL = "full"
    STRUCTURED = "structured"
    BULLETS = "bullets"
    SUMMARY = "summary"
    ONE_LINE = "one_line"

FORMAT_PROMPTS = {
    ResponseFormat.FULL: "",
    ResponseFormat.STRUCTURED: "Respond with: one paragraph explanation, then 3-5 bullet points.",
    ResponseFormat.BULLETS: "Respond with 3-5 bullet points only. No prose.",
    ResponseFormat.SUMMARY: "Respond in 2-3 sentences maximum.",
    ResponseFormat.ONE_LINE: "Respond in exactly one sentence.",
}

FORMAT_MAX_TOKENS = {
    ResponseFormat.FULL: 4096,
    ResponseFormat.STRUCTURED: 1024,
    ResponseFormat.BULLETS: 512,
    ResponseFormat.SUMMARY: 256,
    ResponseFormat.ONE_LINE: 80,
}

def select_format(remaining_tokens: int) -> ResponseFormat:
    if remaining_tokens > 8000:
        return ResponseFormat.FULL
    elif remaining_tokens > 4000:
        return ResponseFormat.STRUCTURED
    elif remaining_tokens > 2000:
        return ResponseFormat.BULLETS
    elif remaining_tokens > 800:
        return ResponseFormat.SUMMARY
    else:
        return ResponseFormat.ONE_LINE

def count_tokens_rough(text: str) -> int:
    return int(len(text.split()) * 1.3)

def tiered_response(
    question: str,
    conversation_history: list[dict],
    system: str = "",
    model: str = "claude-sonnet-4-6",
    context_limit: int = 200_000,
) -> str:
    history_tokens = sum(count_tokens_rough(str(m.get("content", "")))
                         for m in conversation_history)
    system_tokens = count_tokens_rough(system)
    question_tokens = count_tokens_rough(question)
    used = history_tokens + system_tokens + question_tokens + 500  # overhead
    remaining = context_limit - used

    fmt = select_format(remaining)
    format_instruction = FORMAT_PROMPTS[fmt]
    max_tokens = FORMAT_MAX_TOKENS[fmt]

    print(f"[remaining≈{remaining} tokens → format={fmt.value}, max_tokens={max_tokens}]")

    full_question = f"{question}\n\n{format_instruction}" if format_instruction else question
    messages = conversation_history + [{"role": "user", "content": full_question}]

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return response.content[0].text

if __name__ == "__main__":
    # Simulate varying remaining budgets
    for mock_remaining, label in [(15000, "plenty"), (5000, "medium"), (1200, "tight"), (400, "critical")]:
        print(f"\n=== Budget: {label} (~{mock_remaining} tokens) ===")

        # Simulate history that fills up the window
        filler_words = " ".join(["word"] * int((200_000 - mock_remaining - 1000) / 1.3))
        fake_history = [{"role": "user", "content": filler_words[:100]}]  # abbreviated for demo

        fmt = select_format(mock_remaining)
        instruction = FORMAT_PROMPTS[fmt]
        print(f"Format selected: {fmt.value}")
        print(f"Instruction: {instruction or '(unrestricted)'}")

# Expected Token Savings: Up to 98% reduction in response size when budget is critical
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Extended Thinking Budget-Aware Control

Use Claude's extended thinking feature with budget tokens calibrated to remaining context space.

```python
import anthropic

client = anthropic.Anthropic()

def thinking_budget_for_remaining(remaining_tokens: int) -> int:
    """Scale thinking budget to remaining context — never exceed 80% of remaining."""
    if remaining_tokens < 2000:
        return 0   # disable thinking entirely when tight
    elif remaining_tokens < 5000:
        return 1000
    elif remaining_tokens < 15000:
        return 3000
    elif remaining_tokens < 40000:
        return 8000
    else:
        return 16000

def estimate_used_tokens(messages: list[dict], system: str) -> int:
    total = len(system.split()) + 10
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(block.get("text", "").split())
        else:
            total += len(str(content).split())
    return int(total * 1.35)

def budget_aware_thinking_call(
    question: str,
    messages: list[dict],
    system: str = "You are a helpful assistant.",
    context_limit: int = 200_000,
    model: str = "claude-sonnet-4-6",
) -> str:
    used = estimate_used_tokens(messages + [{"role": "user", "content": question}], system)
    remaining = context_limit - used
    thinking_budget = thinking_budget_for_remaining(remaining)

    print(f"[used≈{used}, remaining≈{remaining}, thinking_budget={thinking_budget}]")

    # Response max_tokens scales with remaining, leaving room for thinking
    response_max = min(4096, max(64, remaining - thinking_budget - 500))

    if thinking_budget > 0:
        response = client.messages.create(
            model=model,
            max_tokens=response_max + thinking_budget,
            system=system,
            thinking={"type": "enabled", "budget_tokens": thinking_budget},
            messages=messages + [{"role": "user", "content": question}],
        )
    else:
        # Context too tight for thinking — just answer concisely
        tight_system = system + "\n\nIMPORTANT: Context is almost full. Be extremely brief — 1-2 sentences."
        response = client.messages.create(
            model=model,
            max_tokens=min(150, response_max),
            system=tight_system,
            messages=messages + [{"role": "user", "content": question}],
        )

    # Extract text from response (skip thinking blocks)
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""

if __name__ == "__main__":
    history = [
        {"role": "user", "content": "I'm designing a distributed cache system."},
        {"role": "assistant", "content": "Great! Distributed caches require careful consideration of consistency and eviction."},
    ]
    reply = budget_aware_thinking_call(
        "What cache invalidation strategy should I use for high-write workloads?",
        history,
        system="You are a systems architect.",
    )
    print(reply[:500])

# Expected Token Savings: Disables thinking when budget tight; avoids thinking-overhead truncation
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Sliding Window with Budget-Proportional Summarization

As context fills, summarize older turns into progressively shorter summaries.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

CONTEXT_LIMIT = 200_000
SUMMARY_TRIGGER = 0.70   # summarize when 70% full
SUMMARY_TARGET = 0.40    # compress to 40% usage after summary

def rough_token_count(messages: list[dict]) -> int:
    return int(sum(len(str(m.get("content", "")).split()) for m in messages) * 1.35)

async def summarize_turns(turns: list[dict], target_tokens: int) -> dict:
    """Compress a list of turns into a single summary message."""
    conversation_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in turns
    )
    target_words = int(target_tokens / 1.35)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=target_tokens,
        messages=[{
            "role": "user",
            "content": (
                f"Summarize this conversation in under {target_words} words, "
                f"preserving all key decisions, facts, and context:\n\n{conversation_text}"
            ),
        }],
    )
    return {
        "role": "user",
        "content": f"[CONVERSATION SUMMARY — earlier context compressed]\n{response.content[0].text}",
    }

class BudgetAwareConversation:
    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.model = model
        self.messages: list[dict] = []
        self.system = ""

    async def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})

        # Check if we need to compress
        current_tokens = rough_token_count(self.messages) + rough_token_count([{"role": "system", "content": self.system}])
        usage_ratio = current_tokens / CONTEXT_LIMIT

        if usage_ratio > SUMMARY_TRIGGER and len(self.messages) > 4:
            # Keep last 4 messages verbatim; summarize the rest
            keep_recent = self.messages[-4:]
            to_summarize = self.messages[:-4]
            if to_summarize:
                target = int(CONTEXT_LIMIT * (SUMMARY_TARGET - 0.1) / 1.35)
                print(f"[compressing {len(to_summarize)} turns, usage was {usage_ratio:.0%}]")
                summary_msg = await summarize_turns(to_summarize, target)
                self.messages = [summary_msg] + keep_recent

        # Recalculate remaining after potential compression
        used = rough_token_count(self.messages) + rough_token_count([{"role": "system", "content": self.system}])
        remaining = CONTEXT_LIMIT - used
        max_tokens = min(4096, max(64, int(remaining * 0.4)))

        response = await client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self.system,
            messages=self.messages,
        )
        reply = response.content[0].text
        self.messages.append({"role": "assistant", "content": reply})
        print(f"[messages={len(self.messages)}, used≈{used}, max_tokens={max_tokens}]")
        return reply

async def run_demo():
    conv = BudgetAwareConversation()
    conv.system = "You are a technical advisor helping design a SaaS platform."

    turns = [
        "We're building a multi-tenant SaaS application.",
        "What database strategy do you recommend for multi-tenancy?",
        "We decided on schema-per-tenant. What are the migration challenges?",
        "How should we handle tenant onboarding automation?",
        "What observability stack would you recommend for this?",
        "Now let's talk about the billing integration.",
    ]

    for turn in turns:
        print(f"\nUser: {turn}")
        reply = await conv.chat(turn)
        print(f"Assistant: {reply[:200]}")

if __name__ == "__main__":
    asyncio.run(run_demo())

# Expected Token Savings: Prevents context resets by keeping window 40-70% full; saves full-history re-injection
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Structured Output with Progressive Detail Levels

Use structured output schemas that progressively drop detail fields as budget shrinks.

```python
import json
import anthropic

client = anthropic.Anthropic()

# Full schema — used when budget is plentiful
FULL_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Complete detailed answer"},
        "examples": {"type": "array", "items": {"type": "string"}, "description": "3-5 concrete examples"},
        "tradeoffs": {"type": "string", "description": "Pros and cons"},
        "further_reading": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["answer", "examples", "tradeoffs", "confidence"],
}

# Medium schema — drop further_reading and tradeoffs
MEDIUM_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Concise answer"},
        "examples": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["answer", "confidence"],
}

# Minimal schema — answer only
MINIMAL_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "One-sentence answer"},
    },
    "required": ["answer"],
}

def pick_schema_and_tokens(remaining_tokens: int) -> tuple[dict, int, str]:
    if remaining_tokens > 6000:
        return FULL_SCHEMA, 2048, "full"
    elif remaining_tokens > 2000:
        return MEDIUM_SCHEMA, 768, "medium"
    else:
        return MINIMAL_SCHEMA, 200, "minimal"

def structured_budget_call(question: str, remaining_tokens: int) -> dict:
    schema, max_tokens, level = pick_schema_and_tokens(remaining_tokens)
    print(f"[budget={remaining_tokens} → schema={level}, max_tokens={max_tokens}]")

    prompt = f"Answer this question using the JSON schema provided.\n\nQuestion: {question}"
    if level == "minimal":
        prompt += "\n\nBe extremely brief — one sentence."
    elif level == "medium":
        prompt += "\n\nBe concise — 2-3 sentences max per field."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "name": "answer",
            "description": "Provide structured answer",
            "input_schema": schema,
        }],
        tool_choice={"type": "tool", "name": "answer"},
    )

    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {"answer": "No structured response generated"}

if __name__ == "__main__":
    question = "What is event sourcing and when should I use it?"

    for budget, label in [(15000, "full"), (3000, "medium"), (500, "minimal")]:
        print(f"\n=== Budget: {label} ({budget} tokens) ===")
        result = structured_budget_call(question, budget)
        print(json.dumps(result, indent=2))

# Expected Token Savings: Drops 60-80% of response fields when budget is tight; avoids truncated JSON
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Real-Time Token Tracking with API Usage Feedback

Use actual token counts from API responses to maintain a precise rolling budget across turns.

```python
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class TokenLedger:
    context_limit: int = 200_000
    input_used: int = 0
    output_used: int = 0
    cache_read: int = 0
    cache_create: int = 0
    turns: int = 0

    @property
    def total_used(self) -> int:
        return self.input_used + self.output_used

    @property
    def remaining(self) -> int:
        return max(0, self.context_limit - self.input_used)

    @property
    def pressure(self) -> float:
        return self.input_used / self.context_limit

    def record(self, usage) -> None:
        self.input_used = getattr(usage, "input_tokens", 0)
        self.output_used += getattr(usage, "output_tokens", 0)
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0)
        self.cache_create += getattr(usage, "cache_creation_input_tokens", 0)
        self.turns += 1

    def format_instruction(self) -> str:
        p = self.pressure
        if p < 0.5:
            return ""
        elif p < 0.7:
            return "Be somewhat concise."
        elif p < 0.85:
            return "Be brief — 2-3 sentences maximum."
        elif p < 0.95:
            return "Answer in ONE sentence only."
        else:
            return "CRITICAL: Context almost full. One sentence, 15 words max."

    def max_response_tokens(self) -> int:
        p = self.pressure
        if p < 0.5:
            return 4096
        elif p < 0.7:
            return 2048
        elif p < 0.85:
            return 512
        elif p < 0.95:
            return 150
        else:
            return 50

    def summary(self) -> str:
        return (f"turns={self.turns}, input={self.input_used}, output={self.output_used}, "
                f"remaining≈{self.remaining}, pressure={self.pressure:.1%}")

class PreciseBudgetConversation:
    def __init__(self, system: str, model: str = "claude-sonnet-4-6"):
        self.system = system
        self.model = model
        self.messages: list[dict] = []
        self.ledger = TokenLedger()

    async def turn(self, user_message: str) -> str:
        instruction = self.ledger.format_instruction()
        content = f"{user_message}\n\n{instruction}" if instruction else user_message
        self.messages.append({"role": "user", "content": content})

        max_tokens = self.ledger.max_response_tokens()
        print(f"[{self.ledger.summary()} → max_tokens={max_tokens}]")

        response = await client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self.system,
            messages=self.messages,
        )

        self.ledger.record(response.usage)
        reply = response.content[0].text
        self.messages.append({"role": "assistant", "content": reply})
        return reply

async def run_demo():
    conv = PreciseBudgetConversation(
        system="You are an expert on cloud infrastructure.",
        model="claude-haiku-4-5-20251001",
    )

    questions = [
        "What is Kubernetes?",
        "How does pod scheduling work?",
        "What are resource limits and requests?",
        "Explain horizontal pod autoscaling.",
        "What is a service mesh?",
        "How does Istio compare to Linkerd?",
    ]

    for q in questions:
        print(f"\nQ: {q}")
        reply = await conv.turn(q)
        print(f"A: {reply[:250]}")
        print(f"   [{conv.ledger.summary()}]")

if __name__ == "__main__":
    asyncio.run(run_demo())

# Expected Token Savings: Precise real-time control; response tokens shrink proportionally as window fills
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Approach | Accuracy | Overhead | Best For |
|--------|----------|----------|----------|----------|
| 1 | Prompt injection with word-count instruction | Medium | None | Simple drop-in budget enforcement |
| 2 | Tiered format selection | Medium | None | Distinct response shapes per budget level |
| 3 | Thinking budget calibration | High | Minimal | Reasoning tasks needing extended thinking |
| 4 | Sliding window + summarization | High | 1 cheap call on compress | Long multi-turn conversations |
| 5 | Progressive structured output schemas | High | None | JSON/structured output pipelines |
| 6 | Real-time token tracking via API usage | Highest | None | Production agents needing precise control |
