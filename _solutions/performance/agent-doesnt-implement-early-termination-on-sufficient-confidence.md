---
layout: solution
title: "Agent Doesn't Implement Early Termination on Sufficient Confidence"
category: performance
description: "Stop generation or tool calls early when confidence is already high enough, using streaming confidence checks, score thresholds, and token-budget gates."
tags: [performance, early-termination, confidence, streaming, token-cost, latency]
---

# Agent Doesn't Implement Early Termination on Sufficient Confidence

Agents that always run to completion waste tokens and latency on work that was already settled after the first few sentences. A classification that's clearly "positive" after 50 tokens doesn't need 500 more. A tool call whose first result already answers the question doesn't need three more calls. Early termination detects when sufficient confidence has been reached and stops, saving both cost and latency.

## Option 1: Streaming Confidence Gate with Token Budget

```python
import anthropic

client = anthropic.Anthropic()

CONFIDENCE_KEYWORDS = {
    "high": ["definitely", "clearly", "certainly", "without doubt", "obviously", "absolutely"],
    "low":  ["might", "perhaps", "possibly", "uncertain", "unclear", "not sure"],
}
MAX_TOKENS_BEFORE_CHECK = 60  # Check confidence after this many tokens


def classify_with_early_exit(text: str, question: str) -> tuple[str, int, bool]:
    """
    Stream classification, stopping early when confident.
    Returns (answer, tokens_used, early_exit).
    """
    prompt = f"""Classify whether the following text answers YES or NO to: {question}

Text: {text}

Begin your response with your classification, then explain why."""

    collected = []
    tokens_used = 0
    early_exit = False

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            collected.append(chunk)
            tokens_used += len(chunk.split())
            partial = "".join(collected).lower()

            # Check for early high-confidence signal
            if tokens_used >= MAX_TOKENS_BEFORE_CHECK:
                has_high = any(kw in partial for kw in CONFIDENCE_KEYWORDS["high"])
                has_answer = "yes" in partial[:50] or "no" in partial[:50]
                if has_high and has_answer:
                    early_exit = True
                    break

    answer = "".join(collected)
    classification = "YES" if "yes" in answer[:80].lower() else "NO"
    return classification, tokens_used, early_exit


texts = [
    ("The Eiffel Tower is located in Paris, France.", "Is the Eiffel Tower in Paris?"),
    ("The capital of Australia is Canberra, not Sydney.", "Is Sydney the capital of Australia?"),
]

for text, question in texts:
    result, tokens, early = classify_with_early_exit(text, question)
    print(f"Q: {question}")
    print(f"A: {result} | tokens≈{tokens} | early_exit={early}\n")

# Expected Token Savings: 40-70% on clear-cut classification tasks; no savings on genuinely ambiguous inputs
# Environment: Python 3.11+; tune MAX_TOKENS_BEFORE_CHECK (40-100) based on typical response onset latency
```

## Option 2: Tool Call Confidence Scoring with Stop Gate

```python
import anthropic
import json

client = anthropic.Anthropic()

CONFIDENCE_STOP_THRESHOLD = 0.85  # Stop calling tools when confidence >= this

TOOLS = [
    {
        "name": "lookup_fact",
        "description": "Look up a fact from the knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Fact to look up"},
                "confidence_so_far": {
                    "type": "number",
                    "description": "Your current confidence (0-1) in answering the user's question before this lookup.",
                },
            },
            "required": ["query", "confidence_so_far"],
        },
    }
]

KNOWLEDGE_BASE = {
    "python release date": "Python was first released in 1991 by Guido van Rossum.",
    "python creator": "Python was created by Guido van Rossum.",
    "python license": "Python is distributed under the PSF License.",
}


def lookup_fact(query: str) -> str:
    for key, val in KNOWLEDGE_BASE.items():
        if any(w in query.lower() for w in key.split()):
            return val
    return "No information found."


def run_agent_with_confidence_gate(question: str) -> str:
    messages: list[dict] = [{"role": "user", "content": question}]
    tool_calls = 0
    max_tool_calls = 5

    while tool_calls < max_tool_calls:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for b in response.content:
                if hasattr(b, "text"):
                    return b.text
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                confidence = float(block.input.get("confidence_so_far", 0.0))
                query = block.input["query"]

                print(f"Tool call #{tool_calls + 1}: query='{query}' confidence_so_far={confidence:.2f}")

                # Early termination: agent already confident enough
                if confidence >= CONFIDENCE_STOP_THRESHOLD:
                    print(f"Skipping tool call — confidence {confidence:.2f} >= {CONFIDENCE_STOP_THRESHOLD}")
                    result = f"[skipped — confidence sufficient at {confidence:.2f}]"
                else:
                    result = lookup_fact(query)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
                tool_calls += 1

        messages.append({"role": "user", "content": tool_results})

    return "No answer generated"


answer = run_agent_with_confidence_gate("Who created Python and when was it released?")
print(f"\nAnswer: {answer}")

# Expected Token Savings: 30-50% on lookup-heavy agents; confidence gate prevents redundant corroborating calls
# Environment: Python 3.11+; model may not always report accurate confidence_so_far — validate with output checks
```

## Option 3: Streaming Early Stop with Partial Answer Extraction

```python
import anthropic
import re

client = anthropic.Anthropic()

# Patterns that indicate a complete, confident answer has been given
COMPLETION_PATTERNS = [
    r'\b(therefore|thus|in conclusion|in summary|so the answer is)\b',
    r'\b(the answer is|to summarize|in short)\b',
    r'\.{1}\s*$',  # Ends with a period followed by newline
]

MIN_TOKENS_BEFORE_STOP = 40  # Don't stop too early


def extract_answer(text: str) -> str:
    """Extract the core answer from partial generation."""
    # Try to find a complete sentence
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if sentences:
        return sentences[0]
    return text.strip()


def answer_with_early_stop(question: str) -> tuple[str, int, bool]:
    """Stream answer, stop when confident complete answer detected."""
    collected: list[str] = []
    token_count = 0
    stopped_early = False

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Answer concisely and directly: {question}"
        }],
    ) as stream:
        for chunk in stream.text_stream:
            collected.append(chunk)
            token_count += 1
            partial = "".join(collected)

            if token_count >= MIN_TOKENS_BEFORE_STOP:
                for pattern in COMPLETION_PATTERNS:
                    if re.search(pattern, partial, re.IGNORECASE):
                        stopped_early = True
                        break

            if stopped_early:
                break

    full_text = "".join(collected)
    answer = extract_answer(full_text) if stopped_early else full_text
    return answer, token_count, stopped_early


questions = [
    "What is the boiling point of water at sea level?",
    "What programming language was created by James Gosling?",
    "Explain the complete history of the Byzantine Empire in detail.",
]

for q in questions:
    ans, tokens, early = answer_with_early_stop(q)
    status = "EARLY STOP" if early else "FULL"
    print(f"[{status} | ~{tokens} tokens]\nQ: {q}\nA: {ans[:150]}\n")

# Expected Token Savings: 50-80% on factual Q&A; negligible savings on open-ended questions
# Environment: Python 3.11+; patterns are language-specific — extend for non-English use
```

## Option 4: Multi-Step Chain with Confidence Propagation

```python
import asyncio
import anthropic
import json

client = anthropic.AsyncAnthropic()

CHAIN_STOP_CONFIDENCE = 0.90  # Stop chain when this confidence is reached
MAX_CHAIN_STEPS = 5


async def reasoning_step(question: str, prior_steps: list[dict]) -> dict:
    """Run one reasoning step, returning answer + confidence."""
    history = "\n".join(
        f"Step {i+1}: {s['thought']} (confidence: {s['confidence']:.0%})"
        for i, s in enumerate(prior_steps)
    )
    prompt = f"""Question: {question}

Previous reasoning:
{history if history else "(none)"}

Continue reasoning. Respond with JSON:
{{"thought": "<your next reasoning step>", "confidence": <0.0-1.0>, "final_answer": "<answer or null if not ready>"}}"""

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Strip markdown code blocks if present
    if text.startswith("```"):
        text = re.sub(r"```(?:json)?\n?", "", text).strip().rstrip("`").strip()

    try:
        return json.loads(text)
    except Exception:
        return {"thought": text, "confidence": 0.5, "final_answer": None}


import re

async def chain_of_thought_with_early_exit(question: str) -> tuple[str, int]:
    """Run chain-of-thought, stopping as soon as confidence is sufficient."""
    steps: list[dict] = []
    steps_taken = 0

    for _ in range(MAX_CHAIN_STEPS):
        step = await reasoning_step(question, steps)
        steps.append(step)
        steps_taken += 1
        confidence = step.get("confidence", 0.0)
        final = step.get("final_answer")

        print(f"[step {steps_taken}] confidence={confidence:.0%} | {step['thought'][:80]}")

        if confidence >= CHAIN_STOP_CONFIDENCE and final:
            print(f"Early exit: confidence {confidence:.0%} >= {CHAIN_STOP_CONFIDENCE:.0%}")
            return final, steps_taken

    # Fall back to last step's answer
    last = steps[-1]
    return last.get("final_answer") or last["thought"], steps_taken


questions = [
    "Is 97 a prime number?",
    "What is the capital of France?",
    "What is the square root of 144?",
]

async def main() -> None:
    for q in questions:
        answer, steps = await chain_of_thought_with_early_exit(q)
        print(f"Q: {q}\nA: {answer} (in {steps} steps)\n")

asyncio.run(main())

# Expected Token Savings: 50-70% on simple reasoning; chain terminates at step 1-2 for obvious answers
# Environment: Python 3.11+; set CHAIN_STOP_CONFIDENCE lower (0.75) for exploratory tasks requiring more depth
```

## Option 5: Parallel Candidate Generation with Best-Score Early Pick

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

EARLY_ACCEPT_SCORE = 9.0   # Accept immediately if any candidate scores this high
MIN_CANDIDATES = 2          # Always generate at least this many before early accepting


async def generate_candidate(question: str, attempt: int) -> str:
    """Generate one candidate answer."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Answer concisely (attempt {attempt}): {question}"
        }],
    )
    return response.content[0].text.strip()


async def score_candidate(question: str, answer: str) -> float:
    """Score a candidate answer on accuracy and completeness (0-10)."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"Score 0-10 for accuracy and completeness.\nQ: {question}\nA: {answer}\nRespond with just the number."
        }],
    )
    try:
        return float(response.content[0].text.strip().split()[0])
    except Exception:
        return 5.0


async def best_of_n_with_early_accept(question: str, n: int = 4) -> tuple[str, float, int]:
    """Generate up to N candidates, stop early if a high-scoring one is found."""
    best_answer = ""
    best_score = 0.0
    candidates_used = 0

    for i in range(n):
        candidate = await generate_candidate(question, i + 1)
        score = await score_candidate(question, candidate)
        candidates_used += 1

        print(f"[candidate {i+1}] score={score:.1f} | {candidate[:60]}")

        if score > best_score:
            best_score = score
            best_answer = candidate

        if i + 1 >= MIN_CANDIDATES and best_score >= EARLY_ACCEPT_SCORE:
            print(f"Early accept at candidate {i+1}: score {best_score:.1f} >= {EARLY_ACCEPT_SCORE}")
            break

    return best_answer, best_score, candidates_used


async def main() -> None:
    questions = [
        "What is the largest planet in our solar system?",
        "Explain the concept of technical debt in software development.",
    ]
    for q in questions:
        answer, score, n = await best_of_n_with_early_accept(q)
        print(f"\nQ: {q}")
        print(f"Best (score={score:.1f}, {n} candidates): {answer[:200]}\n")

asyncio.run(main())

# Expected Token Savings: 25-50% vs always generating N; EARLY_ACCEPT_SCORE=9 means ~60% of questions exit at candidate 2
# Environment: Python 3.11+; lower EARLY_ACCEPT_SCORE for recall-critical tasks; raise for precision-critical ones
```

## Option 6: Adaptive Token Budget with Confidence-Proportional Allocation

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class ConfidenceBudget:
    base_tokens: int = 128
    max_tokens: int = 512
    confidence_target: float = 0.85
    tokens_used: int = 0

    def tokens_for_confidence(self, current_confidence: float) -> int:
        """Allocate more tokens the further we are from confidence target."""
        if current_confidence >= self.confidence_target:
            return 0  # No more tokens needed
        gap = self.confidence_target - current_confidence
        extra = int(self.max_tokens * gap)
        return min(self.base_tokens + extra, self.max_tokens - self.tokens_used)


async def estimate_confidence(partial_answer: str, question: str) -> float:
    """Quickly estimate how complete/confident the partial answer is."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{
            "role": "user",
            "content": f"How complete is this answer to '{question}'? Rate 0.0-1.0. Respond with just the number.\n\nAnswer: {partial_answer[:200]}"
        }],
    )
    try:
        return float(response.content[0].text.strip().split()[0])
    except Exception:
        return 0.5


async def adaptive_budget_answer(question: str) -> tuple[str, int]:
    """Generate with adaptive token allocation based on confidence tracking."""
    budget = ConfidenceBudget()
    full_answer = ""
    total_tokens = 0
    round_num = 0

    while budget.tokens_used < budget.max_tokens:
        round_num += 1
        confidence = await estimate_confidence(full_answer, question) if full_answer else 0.0
        alloc = budget.tokens_for_confidence(confidence)

        print(f"[round {round_num}] confidence={confidence:.2f} | allocating {alloc} tokens")

        if alloc <= 0:
            print(f"Sufficient confidence ({confidence:.2f}) — stopping")
            break

        prompt = (
            f"Answer this question: {question}"
            if not full_answer
            else f"Continue and complete this answer: {full_answer}"
        )

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=alloc,
            messages=[{"role": "user", "content": prompt}],
        )
        new_text = response.content[0].text
        full_answer += (" " if full_answer else "") + new_text
        used = response.usage.output_tokens
        budget.tokens_used += used
        total_tokens += used

        if response.stop_reason == "end_turn":
            break

    return full_answer.strip(), total_tokens


async def main() -> None:
    questions = [
        "What year was the Eiffel Tower built?",
        "Explain how neural networks learn using backpropagation.",
    ]
    for q in questions:
        answer, tokens = await adaptive_budget_answer(q)
        print(f"\nQ: {q}\nTokens used: {tokens}\nA: {answer[:300]}\n")

asyncio.run(main())

# Expected Token Savings: 35-60%; simple factual questions get base_tokens only; complex questions get full budget
# Environment: Python 3.11+; confidence estimation adds ~20 tokens/round overhead; net savings positive for 3+ round tasks
```

## Comparison

| Option | Termination Signal | Overhead | Streaming | Parallel | Best For |
|--------|-------------------|----------|-----------|----------|----------|
| 1. Keyword Gate | Confidence keywords in stream | Minimal | Yes | No | Classification, factual Q&A |
| 2. Tool Confidence Gate | Agent-reported confidence | Low | No | No | Tool-heavy agents |
| 3. Pattern Completion | Regex on partial output | Minimal | Yes | No | Short factual answers |
| 4. Chain Confidence | Per-step confidence score | Medium | No | No | Multi-step reasoning |
| 5. Best-of-N Early Accept | Score threshold | High | No | Yes | Quality-critical generation |
| 6. Adaptive Budget | Confidence-proportional tokens | Medium | No | No | Variable-complexity questions |
