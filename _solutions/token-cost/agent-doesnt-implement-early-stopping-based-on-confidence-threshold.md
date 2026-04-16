---
title: "Agent Doesn't Implement Early Stopping Based on Confidence Threshold"
description: "Stop generating tokens as soon as the agent's answer reaches sufficient confidence — avoiding expensive over-generation for simple queries."
category: token-cost
difficulty: intermediate
tags: [token-cost, early-stopping, confidence, streaming, efficiency, cost-reduction]
---

# Agent Doesn't Implement Early Stopping Based on Confidence Threshold

## Problem

Agents generate the full `max_tokens` allocation regardless of query complexity. A simple factual question ("What is 2+2?") should stop after a few tokens, not generate 1024. Without early stopping, agents over-generate on easy queries, wasting tokens and increasing latency. Confidence-based stopping monitors partial output and terminates generation when the answer is already complete.

---

## Option 1: Streaming Early Stop on Sentence Completion

```python
import asyncio
import anthropic
import re

client = anthropic.AsyncAnthropic()

STOP_PATTERNS = [
    r"\.\s*$",           # ends with period
    r"\?\s*$",           # ends with question mark
    r"!\s*$",            # ends with exclamation
    r"\n\n",             # double newline (paragraph break)
]

def looks_complete(text: str, min_length: int = 50) -> bool:
    """Check if the streamed text looks like a complete answer."""
    text = text.strip()
    if len(text) < min_length:
        return False
    return any(re.search(p, text) for p in STOP_PATTERNS)

async def early_stop_stream(
    question: str,
    max_tokens: int = 1024,
    min_answer_length: int = 40,
    check_interval_chars: int = 100,
) -> tuple[str, int, bool]:
    """
    Returns (text, tokens_used, was_early_stopped).
    Streams and stops once the answer looks complete.
    """
    collected: list[str] = []
    total_chars = 0
    early_stopped = False
    token_count = 0

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": question}]
    ) as stream:
        async for event in stream:
            if hasattr(event, "type"):
                if event.type == "content_block_delta" and hasattr(event.delta, "text"):
                    chunk = event.delta.text
                    collected.append(chunk)
                    total_chars += len(chunk)

                    # Check every N characters
                    if total_chars >= check_interval_chars:
                        current_text = "".join(collected)
                        if looks_complete(current_text, min_length=min_answer_length):
                            early_stopped = True
                            break

                elif event.type == "message_delta" and hasattr(event, "usage"):
                    token_count = event.usage.output_tokens

    text = "".join(collected)
    return text, token_count, early_stopped

async def main():
    questions = [
        ("Simple", "What is 7 times 8?"),
        ("Medium", "What is Python's GIL?"),
        ("Complex", "Explain the trade-offs between eventual consistency and strong consistency in distributed systems."),
    ]
    for label, q in questions:
        text, tokens, stopped = await early_stop_stream(q, max_tokens=512, min_answer_length=30)
        print(f"[{label}] Early stopped={stopped}, chars={len(text)}")
        print(f"Answer: {text[:100]}...\n")

asyncio.run(main())
```

---

## Option 2: Two-Phase Generation (Check → Extend)

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def confidence_score(question: str, partial_answer: str) -> float:
    """Use Haiku to score how complete the partial answer is (0.0-1.0)."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system="Score how completely the answer addresses the question. Return only a decimal 0.0-1.0.",
        messages=[
            {"role": "user", "content": f"Question: {question}\nPartial answer: {partial_answer}"}
        ]
    )
    try:
        return float(resp.content[0].text.strip())
    except Exception:
        return 0.5

async def two_phase_generate(
    question: str,
    initial_tokens: int = 150,
    extension_tokens: int = 300,
    confidence_threshold: float = 0.80,
) -> dict:
    """Phase 1: generate short answer. Phase 2: extend only if confidence is low."""
    # Phase 1: Short generation
    resp1 = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=initial_tokens,
        messages=[{"role": "user", "content": question}]
    )
    phase1_text = resp1.content[0].text
    phase1_tokens = resp1.usage.output_tokens
    stop_reason = resp1.stop_reason  # "end_turn" or "max_tokens"

    # If model stopped naturally (end_turn), we're done
    if stop_reason == "end_turn":
        return {
            "answer": phase1_text,
            "tokens_used": phase1_tokens,
            "phases": 1,
            "early_stopped": True,
            "confidence": 1.0
        }

    # Score confidence of partial answer
    conf = await confidence_score(question, phase1_text)
    print(f"[PHASE 1] tokens={phase1_tokens}, confidence={conf:.2f}")

    if conf >= confidence_threshold:
        return {
            "answer": phase1_text,
            "tokens_used": phase1_tokens,
            "phases": 1,
            "early_stopped": True,
            "confidence": conf
        }

    # Phase 2: Extend the answer
    resp2 = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=extension_tokens,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": phase1_text},
            {"role": "user", "content": "Please continue and complete your answer."}
        ]
    )
    phase2_text = resp2.content[0].text
    total_tokens = phase1_tokens + resp2.usage.output_tokens

    return {
        "answer": phase1_text + " " + phase2_text,
        "tokens_used": total_tokens,
        "phases": 2,
        "early_stopped": False,
        "confidence": conf
    }

async def main():
    questions = [
        "What is 100 divided by 4?",
        "What is machine learning?",
        "Explain the Byzantine Generals Problem and its implications for blockchain consensus.",
    ]
    for q in questions:
        result = await two_phase_generate(q, initial_tokens=100, confidence_threshold=0.75)
        print(f"Q: {q}")
        print(f"Phases: {result['phases']}, Tokens: {result['tokens_used']}, Early stopped: {result['early_stopped']}")
        print(f"Answer: {result['answer'][:120]}\n")

asyncio.run(main())
```

---

## Option 3: Adaptive max_tokens Based on Query Complexity

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class ComplexityBand:
    name: str
    max_tokens: int
    keywords: list[str]

COMPLEXITY_BANDS = [
    ComplexityBand("trivial", 50, ["what is", "define", "how many", "when was", "who is"]),
    ComplexityBand("simple", 150, ["explain", "describe", "summarize", "list"]),
    ComplexityBand("medium", 400, ["compare", "contrast", "analyze", "pros and cons"]),
    ComplexityBand("complex", 800, ["design", "implement", "trade-offs", "architecture", "comprehensive"]),
    ComplexityBand("expert", 1500, ["in-depth", "detailed analysis", "thorough", "exhaustive"]),
]

def classify_query(query: str) -> ComplexityBand:
    q_lower = query.lower()
    # Check from most complex to least (first match wins from bottom)
    for band in reversed(COMPLEXITY_BANDS):
        if any(kw in q_lower for kw in band.keywords):
            return band
    return COMPLEXITY_BANDS[1]  # default: simple

async def adaptive_tokens_call(query: str) -> dict:
    band = classify_query(query)
    print(f"[ADAPTIVE] Complexity: {band.name}, max_tokens: {band.max_tokens}")
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=band.max_tokens,
        messages=[{"role": "user", "content": query}]
    )
    return {
        "answer": resp.content[0].text,
        "complexity": band.name,
        "max_tokens_allocated": band.max_tokens,
        "tokens_used": resp.usage.output_tokens,
        "efficiency": resp.usage.output_tokens / band.max_tokens
    }

async def main():
    queries = [
        "What is 5 squared?",
        "Explain what a REST API is.",
        "Compare SQL and NoSQL databases.",
        "Design a distributed rate limiter.",
        "Provide an in-depth analysis of CAP theorem implications for microservices.",
    ]
    for q in queries:
        result = await adaptive_tokens_call(q)
        print(f"Q: {q[:60]}")
        print(f"  {result['complexity']} | allocated={result['max_tokens_allocated']} used={result['tokens_used']} efficiency={result['efficiency']:.1%}")

asyncio.run(main())
```

---

## Option 4: Streaming with Keyword-Based Stop Signals

```python
import asyncio
import anthropic
import re
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class StopSignal:
    pattern: str
    description: str
    compiled: re.Pattern = None

    def __post_init__(self):
        self.compiled = re.compile(self.pattern, re.IGNORECASE | re.MULTILINE)

STOP_SIGNALS = [
    StopSignal(r"^(yes|no|true|false)\s*$", "boolean answer"),
    StopSignal(r"^\d+(\.\d+)?\s*$", "numeric answer"),
    StopSignal(r"in summary[,:]", "summary reached"),
    StopSignal(r"to conclude[,:]", "conclusion reached"),
    StopSignal(r"therefore[,:]?\s+.{20,}\.", "conclusion with reasoning"),
    StopSignal(r"\.\s*(hope this helps|let me know|feel free)", "conversational closer"),
]

def detect_stop_signal(text: str) -> tuple[bool, str | None]:
    for signal in STOP_SIGNALS:
        if signal.compiled.search(text):
            return True, signal.description
    return False, None

async def keyword_stop_stream(
    question: str,
    max_tokens: int = 1024,
    check_every_n_chars: int = 80,
) -> tuple[str, bool, str | None]:
    """Returns (text, was_stopped, stop_reason)."""
    parts: list[str] = []
    chars_since_check = 0
    stop_reason: str | None = None

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": question}]
    ) as stream:
        async for event in stream:
            if hasattr(event, "type") and event.type == "content_block_delta" and hasattr(event.delta, "text"):
                chunk = event.delta.text
                parts.append(chunk)
                chars_since_check += len(chunk)

                if chars_since_check >= check_every_n_chars:
                    chars_since_check = 0
                    current = "".join(parts)
                    should_stop, reason = detect_stop_signal(current)
                    if should_stop:
                        stop_reason = reason
                        break

    return "".join(parts), stop_reason is not None, stop_reason

async def main():
    questions = [
        "Is Python interpreted or compiled?",
        "What is 15% of 200?",
        "Therefore, explain briefly: what is asyncio in Python?",
        "In summary, how does asyncio work in Python?",
    ]
    for q in questions:
        text, stopped, reason = await keyword_stop_stream(q)
        print(f"Q: {q}")
        print(f"  Stopped: {stopped} ({reason}), length: {len(text)} chars")
        print(f"  Answer: {text[:100]}\n")

asyncio.run(main())
```

---

## Option 5: Token Budget Escalation with Automatic Re-attempt

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

TOKEN_LADDER = [100, 250, 500, 1000, 2000]

async def is_truncated(text: str, stop_reason: str) -> bool:
    """Check if the response was cut off mid-thought."""
    if stop_reason == "end_turn":
        return False
    # Heuristic: ends abruptly without punctuation
    stripped = text.strip()
    return not (stripped.endswith((".", "!", "?", ":", "```", "---")) or len(stripped) < 20)

async def escalating_call(
    question: str,
    starting_tokens: int = 100,
    max_escalations: int = 4,
) -> dict:
    """Start with small token budget, escalate only if response is truncated."""
    ladder = [t for t in TOKEN_LADDER if t >= starting_tokens]

    for i, token_limit in enumerate(ladder[:max_escalations + 1]):
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=token_limit,
            messages=[{"role": "user", "content": question}]
        )
        text = resp.content[0].text
        tokens_used = resp.usage.output_tokens
        truncated = await is_truncated(text, resp.stop_reason)

        print(f"[ESCALATE] Attempt {i+1}: limit={token_limit}, used={tokens_used}, truncated={truncated}")

        if not truncated:
            return {
                "answer": text,
                "tokens_used": tokens_used,
                "final_limit": token_limit,
                "escalations": i,
                "truncated": False
            }

    # Final attempt — return whatever we have
    return {
        "answer": text,
        "tokens_used": tokens_used,
        "final_limit": ladder[min(max_escalations, len(ladder)-1)],
        "escalations": max_escalations,
        "truncated": True
    }

async def main():
    questions = [
        "Yes or no: Is Python dynamically typed?",
        "What is dependency injection?",
        "Explain the SOLID principles of object-oriented design with examples.",
    ]
    for q in questions:
        result = await escalating_call(q, starting_tokens=80)
        print(f"Q: {q[:60]}")
        print(f"  Tokens: {result['tokens_used']}, Escalations: {result['escalations']}, Final limit: {result['final_limit']}")
        print(f"  Answer: {result['answer'][:100]}\n")

asyncio.run(main())
```

---

## Option 6: Concurrent Confidence Sampling with Fastest-Done Wins

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class SampledResponse:
    text: str
    tokens: int
    latency_ms: float
    max_tokens_used: int

async def sample_response(question: str, max_tokens: int) -> SampledResponse:
    t0 = time.time()
    resp = await client.messages.create(
        model="claude-sonnet-4-6" if max_tokens > 200 else "claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": question}]
    )
    return SampledResponse(
        text=resp.content[0].text,
        tokens=resp.usage.output_tokens,
        latency_ms=(time.time() - t0) * 1000,
        max_tokens_used=max_tokens
    )

def answer_quality_heuristic(text: str, question: str) -> float:
    """Fast heuristic: longer answers to complex questions are better (up to a point)."""
    question_words = len(question.split())
    answer_words = len(text.split())
    # Ideal ratio: ~3-8 words per question word for medium questions
    ideal_words = question_words * 5
    ratio = answer_words / max(ideal_words, 1)
    # Score peaks at ratio=1.0, falls off for very short or very long
    import math
    return math.exp(-0.5 * (ratio - 1.0) ** 2)

async def concurrent_confidence_call(question: str) -> dict:
    """
    Fire 3 concurrent attempts with different token budgets.
    Pick the one that finishes first AND scores above threshold.
    """
    token_budgets = [100, 350, 800]
    threshold = 0.3  # minimum quality score to accept
    done_event = asyncio.Event()
    winner: list[SampledResponse] = []
    lock = asyncio.Lock()

    async def attempt(max_tokens: int):
        try:
            result = await sample_response(question, max_tokens)
            score = answer_quality_heuristic(result.text, question)
            async with lock:
                if not done_event.is_set() and score >= threshold:
                    done_event.set()
                    winner.append(result)
                    if max_tokens != max(token_budgets):
                        print(f"[CONCURRENT] Early win at max_tokens={max_tokens}, score={score:.2f}, latency={result.latency_ms:.0f}ms")
        except Exception as e:
            print(f"[CONCURRENT] attempt {max_tokens} failed: {e}")

    tasks = [asyncio.create_task(attempt(t)) for t in token_budgets]
    try:
        await asyncio.wait_for(done_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        pass

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if not winner:
        raise RuntimeError("All concurrent attempts failed")

    w = winner[0]
    return {"answer": w.text, "tokens": w.tokens, "latency_ms": w.latency_ms, "budget": w.max_tokens_used}

async def main():
    questions = [
        "What year was Python created?",
        "Explain the difference between a list and a tuple in Python.",
        "What are the key design principles behind RESTful APIs?",
    ]
    for q in questions:
        result = await concurrent_confidence_call(q)
        print(f"Q: {q}")
        print(f"  Budget used: {result['budget']}, Tokens: {result['tokens']}, Latency: {result['latency_ms']:.0f}ms")
        print(f"  Answer: {result['answer'][:100]}\n")

asyncio.run(main())
```

---

## Comparison

| Option | Stop Mechanism | API Calls | Latency Impact | Best For |
|--------|--------------|-----------|----------------|----------|
| 1 – Streaming Pattern | Regex on stream | 1 | Minimal | General over-generation |
| 2 – Two-Phase | Haiku confidence scorer | 2 (sometimes) | +50-200ms | High-value accuracy balance |
| 3 – Adaptive max_tokens | Keyword complexity | 1 | None | Predictable query types |
| 4 – Keyword Stop Signal | Regex on stream | 1 | Minimal | Conversational agents |
| 5 – Escalating Budget | Stop reason check | 1-5 | Proportional | Unknown complexity workloads |
| 6 – Concurrent Sampling | Quality heuristic | 2-3 parallel | Minimal (parallel) | Latency-sensitive with budget flexibility |

**Recommendation:** Use Option 3 (adaptive max_tokens) as your primary strategy — zero overhead, immediate cost reduction for predictable workloads. Layer Option 1 (streaming pattern stop) to catch completions mid-stream. Use Option 5 (escalating budget) when query complexity is unpredictable and you want to guarantee complete answers without over-allocating by default.
