---
layout: solution
title: "Chain-of-Thought Reasoning Makes Agent Responses Too Verbose"
category: prompt-engineering
description: "Agent uses chain-of-thought and shows all its reasoning steps in every response. Simple questions get 1,000-word answers with 'First, let me consider...', 'Step 1:', 'Therefore...'. Responses are slow and costly."
tags: [chain-of-thought, verbose, reasoning, token-cost, performance, output-format]
---

# Chain-of-Thought Reasoning Makes Agent Responses Too Verbose

## Symptom
- "What's 2+2?" returns a 400-word reasoning chain before answering "4"
- Every response starts with "Let me think through this step by step..."
- API costs are 3-5× higher than expected due to long outputs
- Users complain responses are too long and hard to read
- Response latency is high because the model writes out its full reasoning

## Root Cause
Chain-of-thought (CoT) improves reasoning accuracy on complex problems but is overkill for simple tasks and costly in production. When CoT is enabled broadly, every request — simple or complex — gets the full reasoning treatment, wasting tokens and time.

## Fix

### Option 1: Separate thinking from output using extended thinking

```python
import anthropic

client = anthropic.Anthropic()

def answer_with_thinking(question: str) -> str:
    """Use extended thinking for accuracy but return only final answer"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        thinking={
            "type": "enabled",
            "budget_tokens": 10000  # Allow up to 10K tokens of internal reasoning
        },
        messages=[{"role": "user", "content": question}]
    )

    # Extract only the text response, not the thinking blocks
    for block in response.content:
        if block.type == "text":
            return block.text  # Just the answer, no reasoning chain visible

    return ""
```

This gives you the reasoning benefit without showing it in the output.

### Option 2: Explicit output format instructions

```
System prompt:
"Response format:
- Answer simple factual questions in 1-3 sentences
- Answer complex technical questions with a short explanation (under 200 words)
- Show step-by-step reasoning ONLY when explicitly asked or when solving multi-step math/logic
- Never begin with 'Let me think through this' or 'First, let me consider'
- Lead with the answer, not the reasoning
- Reasoning is for YOU internally — the user sees only conclusions"
```

### Option 3: Selective CoT based on question complexity

```python
import anthropic

client = anthropic.Anthropic()

SIMPLE_QUESTION_PATTERNS = [
    r"what is \d+",
    r"^(yes|no|true|false)\?",
    r"^(what|who|when|where) (is|are|was|were)",
    r"^(define|spell|translate)",
]

def needs_chain_of_thought(question: str) -> bool:
    """Only use CoT for genuinely complex questions"""
    import re
    question_lower = question.lower()

    # Simple patterns don't need CoT
    for pattern in SIMPLE_QUESTION_PATTERNS:
        if re.match(pattern, question_lower):
            return False

    # Complexity indicators
    complex_indicators = [
        "step by step", "how do i", "explain why", "prove that",
        "debug", "analyze", "compare", "design", "calculate"
    ]
    return any(ind in question_lower for ind in complex_indicators)

def answer(question: str) -> str:
    if needs_chain_of_thought(question):
        system = "Think through this carefully step by step before answering."
    else:
        system = "Answer directly and concisely."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text
```

### Option 4: Structured output to enforce conciseness

```python
def answer_structured(question: str) -> dict:
    """Force structured output to separate reasoning from answer"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"""Answer this question. Return JSON only:
{{
  "answer": "direct answer in 1-2 sentences",
  "confidence": "high/medium/low",
  "reasoning": "brief explanation if needed, else null"
}}

Question: {question}"""
        }]
    )

    import json
    return json.loads(response.content[0].text)

result = answer_structured("What is the capital of France?")
# {"answer": "Paris", "confidence": "high", "reasoning": null}
# → Show only result["answer"] to user
```

### Option 5: Token budget for output

```python
def answer_with_budget(question: str, is_simple: bool = False) -> str:
    max_tokens = 150 if is_simple else 1024

    prompt_suffix = (
        "\n\nAnswer in 1-2 sentences only." if is_simple
        else "\n\nBe thorough but concise. Under 300 words."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": question + prompt_suffix}]
    )
    return response.content[0].text
```

### Option 6: Strip reasoning from output in post-processing

```python
import re

def strip_reasoning_from_output(text: str) -> str:
    """Remove common CoT preambles from output"""
    # Remove "Let me think through this..." paragraphs
    patterns = [
        r"Let me (think|consider|analyze|work through).*?\n\n",
        r"Step \d+:.*?(?=Step \d+:|Therefore|Thus|In conclusion|$)",
        r"First,.*?Second,.*?(?=Therefore|Thus|$)",
        r"^(Therefore|Thus|In conclusion|To summarize|In summary),?\s*",
    ]

    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.DOTALL | re.IGNORECASE)

    return text.strip()

# Post-process agent response
raw_response = agent.complete(question)
clean_response = strip_reasoning_from_output(raw_response)
```

## When CoT Helps vs. Hurts

| Task type | CoT needed? | Better approach |
|---|---|---|
| Math / logic puzzles | Yes | Extended thinking |
| Multi-step planning | Yes | CoT or extended thinking |
| Simple factual Q&A | No | Direct answer instruction |
| Code generation | Sometimes | CoT for complex algorithms |
| Translation | No | Direct output |
| Classification | Rarely | Direct label output |
| Debugging | Yes | CoT helpful for diagnosis |
| Data extraction | No | Structured JSON output |

## Token Cost of CoT

| Approach | Avg output tokens | Relative cost |
|---|---|---|
| Direct answer | 50–100 | 1× |
| Brief CoT | 200–400 | 3–4× |
| Full CoT | 500–2000 | 10–40× |
| Extended thinking (hidden) | Varies (not billed same) | Check docs |

## Expected Token Savings
Unnecessary CoT on all queries × 100K calls: ~50M extra output tokens
Selective CoT only where needed: 80–90% reduction

## Environment
- Any agent with complex system prompts that accidentally enable CoT for all queries
- Source: direct experience and measurement of CoT token overhead
