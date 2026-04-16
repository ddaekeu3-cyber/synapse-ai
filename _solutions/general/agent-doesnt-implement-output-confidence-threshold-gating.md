---
layout: solution
title: "Agent Doesn't Implement Output Confidence Threshold Gating"
category: general
description: "Gate agent output on a confidence threshold so that low-confidence responses trigger clarification, abstention, escalation, or human review instead of silently delivering unreliable answers."
tags: [confidence, gating, reliability, hallucination, escalation, uncertainty, output-quality]
---

# Agent Doesn't Implement Output Confidence Threshold Gating

## Problem

An agent that always returns a response regardless of its internal uncertainty will deliver low-confidence answers with the same confident tone as high-confidence ones. Users cannot distinguish reliable from unreliable output, leading to downstream mistakes. Implementing confidence threshold gating — where uncertain outputs trigger alternative behaviors — creates a trustworthy, self-aware agent.

## Solutions

### Option 1: Ask for Clarification When Uncertain

When the agent detects low confidence, it asks the user a targeted clarifying question instead of guessing.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

CONFIDENCE_THRESHOLD = 0.75


def assess_and_respond(question: str, context: str = "") -> dict:
    prompt = (
        f"{'Context: ' + context + chr(10) if context else ''}"
        f"Question: {question}\n\n"
        "Before answering, assess your confidence in providing an accurate, complete response.\n"
        "Respond with JSON:\n"
        "{\n"
        '  "confidence": 0.0-1.0,\n'
        '  "uncertainty_reason": "why you are uncertain, or empty string",\n'
        '  "clarifying_question": "a targeted question to resolve uncertainty, or empty string",\n'
        '  "answer": "your answer if confidence >= 0.75, else empty string"\n'
        "}"
    )

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"action": "answer", "content": raw, "confidence": 1.0}

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return {"action": "answer", "content": raw, "confidence": 1.0}

    confidence = float(data.get("confidence", 0.5))

    if confidence >= CONFIDENCE_THRESHOLD:
        return {
            "action": "answer",
            "content": data.get("answer", raw),
            "confidence": confidence,
        }
    else:
        return {
            "action": "clarify",
            "content": data.get(
                "clarifying_question",
                "Could you provide more details so I can give an accurate answer?"
            ),
            "confidence": confidence,
            "uncertainty_reason": data.get("uncertainty_reason", ""),
        }


def interactive_session(questions: list[tuple[str, str]]) -> None:
    for question, context in questions:
        result = assess_and_respond(question, context)
        print(f"Q: {question}")
        print(f"Action: {result['action']} (confidence={result['confidence']:.2f})")
        if result["action"] == "clarify":
            print(f"Clarification needed: {result['content']}")
            print(f"Reason: {result.get('uncertainty_reason', '')}")
        else:
            print(f"Answer: {result['content'][:150]}...")
        print()


if __name__ == "__main__":
    test_cases = [
        ("What is 2 + 2?", ""),
        ("What was the exact revenue of Acme Corp in Q3 2019?", ""),
        ("How do I configure NGINX for SSL termination?", "Ubuntu 22.04, port 443"),
        ("What will the stock price of XYZ be tomorrow?", ""),
    ]
    interactive_session(test_cases)

# Expected Token Savings: Avoids wasted tokens on wrong answers; slight overhead for assessment
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Abstain with Explanation on Low Confidence

Return a structured abstention explaining what the agent does and does not know, rather than fabricating an answer.

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

ABSTAIN_THRESHOLD    = 0.6
UNCERTAIN_THRESHOLD  = 0.8


@dataclass
class GatedResponse:
    action: str          # "answer" | "uncertain" | "abstain"
    content: str
    confidence: float
    known_facts: list[str]
    unknown_aspects: list[str]


def gated_response(question: str) -> GatedResponse:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=768,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                "Analyze what you know and don't know about this question. Respond with JSON:\n"
                "{\n"
                '  "confidence": 0.0-1.0,\n'
                '  "known_facts": ["fact1", "fact2"],\n'
                '  "unknown_aspects": ["what I don\'t know"],\n'
                '  "answer": "full answer if confident, partial answer if uncertain, empty if abstaining"\n'
                "}"
            ),
        }],
    )

    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    data = {}
    if match:
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            pass

    confidence    = float(data.get("confidence", 0.5))
    known_facts   = data.get("known_facts", [])
    unknown_aspects = data.get("unknown_aspects", [])
    answer        = data.get("answer", "")

    if confidence >= UNCERTAIN_THRESHOLD:
        action  = "answer"
        content = answer or raw
    elif confidence >= ABSTAIN_THRESHOLD:
        action  = "uncertain"
        known_str   = "\n".join(f"• {f}" for f in known_facts) if known_facts else "None identified"
        unknown_str = "\n".join(f"• {u}" for u in unknown_aspects) if unknown_aspects else "None identified"
        content = (
            f"I can partially answer this with moderate confidence ({confidence:.0%}).\n\n"
            f"What I know:\n{known_str}\n\n"
            f"What I'm uncertain about:\n{unknown_str}\n\n"
            f"Partial answer: {answer}"
        )
    else:
        action      = "abstain"
        known_str   = "\n".join(f"• {f}" for f in known_facts) if known_facts else "None"
        unknown_str = "\n".join(f"• {u}" for u in unknown_aspects) if unknown_aspects else "None"
        content = (
            f"I don't have sufficient confidence ({confidence:.0%}) to answer this reliably.\n\n"
            f"What I do know:\n{known_str}\n\n"
            f"What I cannot determine:\n{unknown_str}\n\n"
            "I recommend consulting an authoritative source for this information."
        )

    return GatedResponse(
        action=action,
        content=content,
        confidence=confidence,
        known_facts=known_facts,
        unknown_aspects=unknown_aspects,
    )


if __name__ == "__main__":
    questions = [
        "What is the capital of France?",
        "What is the population of a small village in rural Romania called Pietroasele?",
        "What will GDP growth be in 2026?",
        "How does TCP/IP three-way handshake work?",
    ]
    for q in questions:
        result = gated_response(q)
        print(f"Q: {q}")
        print(f"Action: {result.action} (confidence={result.confidence:.2f})")
        print(f"Response: {result.content[:200]}...\n")

# Expected Token Savings: Prevents multi-turn correction cycles; upfront cost saves downstream waste
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Regenerate with Diversity Sampling on Low Confidence

When confidence is low, generate multiple diverse candidates and pick the one with highest self-assessed reliability.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

NUM_CANDIDATES     = 3
CONFIDENCE_FLOOR   = 0.7


def generate_candidate(question: str, attempt: int) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n"
                f"Attempt #{attempt + 1} — provide your best answer and rate your confidence.\n"
                'Respond with JSON: {"answer": "...", "confidence": 0.0-1.0, "reasoning": "..."}'
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return {
                "answer":     data.get("answer", raw),
                "confidence": float(data.get("confidence", 0.5)),
                "reasoning":  data.get("reasoning", ""),
            }
        except (json.JSONDecodeError, ValueError):
            pass
    return {"answer": raw, "confidence": 0.5, "reasoning": ""}


def select_best_candidate(question: str, candidates: list[dict]) -> dict:
    if not candidates:
        return {"answer": "No candidates.", "confidence": 0.0}

    # self-evaluation: ask model to pick the best candidate
    candidates_text = "\n\n".join(
        f"Candidate {i+1} (confidence={c['confidence']:.2f}):\n{c['answer']}"
        for i, c in enumerate(candidates)
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"{candidates_text}\n\n"
                "Which candidate is most accurate and complete? Reply with JSON: "
                '{"best_index": 0-based integer, "combined_confidence": 0.0-1.0}'
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    best_idx = 0
    combined_conf = max(c["confidence"] for c in candidates)
    if match:
        try:
            data = json.loads(match.group())
            best_idx = int(data.get("best_index", 0))
            combined_conf = float(data.get("combined_confidence", combined_conf))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    best_idx = max(0, min(best_idx, len(candidates) - 1))
    return {**candidates[best_idx], "confidence": combined_conf}


def diversity_gated_response(question: str) -> dict:
    # first attempt
    first = generate_candidate(question, 0)

    if first["confidence"] >= CONFIDENCE_FLOOR:
        return {**first, "strategy": "single_pass", "attempts": 1}

    # low confidence — generate more candidates
    candidates = [first] + [generate_candidate(question, i + 1) for i in range(NUM_CANDIDATES - 1)]
    best = select_best_candidate(question, candidates)

    return {
        **best,
        "strategy": "diversity_sampling",
        "attempts": NUM_CANDIDATES,
        "all_confidences": [c["confidence"] for c in candidates],
    }


if __name__ == "__main__":
    questions = [
        "What is the Pythagorean theorem?",
        "What are the exact lyrics of a traditional Mongolian folk song?",
        "Explain how gradient descent works in neural networks.",
        "What is the middle name of the 34th person to walk on the Moon?",
    ]
    for q in questions:
        result = diversity_gated_response(q)
        print(f"Q: {q}")
        print(f"Strategy: {result['strategy']} | Attempts: {result['attempts']} | Confidence: {result['confidence']:.2f}")
        if "all_confidences" in result:
            print(f"All confidences: {result['all_confidences']}")
        print(f"Answer: {result['answer'][:150]}...\n")

# Expected Token Savings: 1x on high-confidence, 3x on uncertain; net saves downstream corrections
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Escalate to Stronger Model on Low Confidence

Automatically retry with a more capable model when the primary model reports low confidence.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

MODEL_LADDER = [
    ("claude-haiku-4-5-20251001", 0.80),   # (model, min_confidence_to_accept)
    ("claude-sonnet-4-6",         0.80),
    ("claude-opus-4-6",           0.0),    # always accept opus result
]


def attempt_with_model(question: str, model: str) -> dict:
    resp = client.messages.create(
        model=model,
        max_tokens=768,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                "Answer as accurately as possible, then rate your confidence.\n"
                'Respond with JSON: {"answer": "...", "confidence": 0.0-1.0, "caveats": "any important caveats"}'
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return {
                "answer":     data.get("answer", raw),
                "confidence": float(data.get("confidence", 0.5)),
                "caveats":    data.get("caveats", ""),
                "model":      model,
            }
        except (json.JSONDecodeError, ValueError):
            pass
    return {"answer": raw, "confidence": 0.5, "caveats": "", "model": model}


def escalating_response(question: str) -> dict:
    history: list[dict] = []

    for model, threshold in MODEL_LADDER:
        result = attempt_with_model(question, model)
        history.append(result)
        print(f"  [{model}] confidence={result['confidence']:.2f}")

        if result["confidence"] >= threshold:
            return {
                **result,
                "escalated": len(history) > 1,
                "escalation_path": [h["model"] for h in history],
            }

    # return last (opus) result
    return {
        **history[-1],
        "escalated": True,
        "escalation_path": [h["model"] for h in history],
    }


if __name__ == "__main__":
    questions = [
        "What is the formula for the area of a circle?",
        "Explain the nuances of quantum decoherence in macroscopic systems.",
        "What was the exact text of the Treaty of Westphalia Article XVII?",
        "How do I reverse a linked list in Python?",
    ]
    for q in questions:
        print(f"Q: {q}")
        result = escalating_response(q)
        print(f"Final model: {result['model']} | Confidence: {result['confidence']:.2f} | Escalated: {result['escalated']}")
        if result["escalation_path"]:
            print(f"Escalation path: {' → '.join(result['escalation_path'])}")
        print(f"Answer: {result['answer'][:150]}...")
        if result["caveats"]:
            print(f"Caveats: {result['caveats'][:100]}")
        print()

# Expected Token Savings: Haiku for easy questions saves 10x vs. always using Opus
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Uncertainty Markers in Structured Output

Return structured output with per-field confidence markers so downstream systems can act on uncertainty at field granularity.

```python
import anthropic
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any

client = anthropic.Anthropic()

FIELD_CONFIDENCE_THRESHOLD = 0.7


@dataclass
class ConfidentField:
    value: Any
    confidence: float
    source_hint: str = ""

    @property
    def is_reliable(self) -> bool:
        return self.confidence >= FIELD_CONFIDENCE_THRESHOLD


@dataclass
class GatedStructuredOutput:
    fields: dict[str, ConfidentField] = field(default_factory=dict)
    overall_confidence: float = 0.0
    unreliable_fields: list[str] = field(default_factory=list)


def extract_structured_with_confidence(question: str, schema: dict) -> GatedStructuredOutput:
    field_list = "\n".join(
        f'  "{k}": {{"value": ..., "confidence": 0.0-1.0, "source_hint": "..."}}'
        for k in schema.keys()
    )
    prompt = (
        f"Question: {question}\n\n"
        f"Extract the following fields and for each, rate your confidence (0.0–1.0) "
        f"and note where the information comes from.\n\n"
        f"Respond with JSON:\n{{\n{field_list}\n}}"
    )

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=768,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    output = GatedStructuredOutput()

    if not match:
        return output

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return output

    confidences: list[float] = []
    for key in schema.keys():
        entry = data.get(key, {})
        if not isinstance(entry, dict):
            entry = {"value": entry, "confidence": 0.5, "source_hint": ""}

        conf = float(entry.get("confidence", 0.5))
        cf = ConfidentField(
            value=entry.get("value"),
            confidence=conf,
            source_hint=str(entry.get("source_hint", "")),
        )
        output.fields[key] = cf
        confidences.append(conf)
        if not cf.is_reliable:
            output.unreliable_fields.append(key)

    output.overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return output


def render_output(output: GatedStructuredOutput) -> None:
    print(f"Overall confidence: {output.overall_confidence:.2f}")
    if output.unreliable_fields:
        print(f"WARNING: Low-confidence fields: {output.unreliable_fields}")
    print("\nFields:")
    for name, cf in output.fields.items():
        status = "OK" if cf.is_reliable else "LOW"
        print(f"  [{status}] {name}: {cf.value!r} (conf={cf.confidence:.2f}, src={cf.source_hint[:40]})")


if __name__ == "__main__":
    queries = [
        (
            "Tell me about the Eiffel Tower.",
            {"height_meters": "number", "year_built": "number", "architect": "name", "annual_visitors_2023": "number"},
        ),
        (
            "What do you know about the fictional company Acme Inc.?",
            {"founded": "year", "ceo": "name", "revenue": "USD", "employees": "number"},
        ),
    ]
    for question, schema in queries:
        print(f"\nQ: {question}")
        output = extract_structured_with_confidence(question, schema)
        render_output(output)

# Expected Token Savings: Downstream systems skip unreliable fields, reducing retry calls
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Human-in-the-Loop Review Queue for Low-Confidence Output

Route low-confidence outputs to a human review queue instead of delivering them directly, with async processing.

```python
import anthropic
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from collections import deque

client = anthropic.AsyncAnthropic()

CONFIDENCE_THRESHOLD = 0.75
REVIEW_QUEUE_TIMEOUT = 5.0   # seconds to wait for simulated human review


@dataclass
class PendingReview:
    request_id: str
    question: str
    draft_answer: str
    confidence: float
    timestamp: float = field(default_factory=time.time)
    reviewed_answer: str | None = None
    reviewed: asyncio.Event = field(default_factory=asyncio.Event)


class ReviewQueue:
    def __init__(self) -> None:
        self._queue: deque[PendingReview] = deque()
        self._by_id: dict[str, PendingReview] = {}

    def enqueue(self, item: PendingReview) -> None:
        self._queue.append(item)
        self._by_id[item.request_id] = item
        print(f"  [REVIEW QUEUE] #{item.request_id} queued (conf={item.confidence:.2f})")

    async def simulate_human_review(self, item: PendingReview) -> None:
        """Simulate a human reviewing and approving/correcting the draft."""
        await asyncio.sleep(0.1)   # simulated review latency
        # In production: webhook, Slack message, UI, etc.
        item.reviewed_answer = f"[Human-reviewed] {item.draft_answer}"
        item.reviewed.set()
        print(f"  [REVIEW QUEUE] #{item.request_id} reviewed by human")

    async def process_next(self) -> None:
        if self._queue:
            item = self._queue.popleft()
            await self.simulate_human_review(item)


review_queue = ReviewQueue()


async def generate_with_confidence(question: str, request_id: str) -> dict:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                "Answer this question and provide a confidence score.\n"
                'Respond with JSON: {"answer": "...", "confidence": 0.0-1.0}'
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    data = {}
    if match:
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {
        "answer":     data.get("answer", raw),
        "confidence": float(data.get("confidence", 0.5)),
    }


async def gated_answer(question: str, request_id: str) -> dict:
    result = await generate_with_confidence(question, request_id)

    if result["confidence"] >= CONFIDENCE_THRESHOLD:
        return {
            "request_id":  request_id,
            "answer":      result["answer"],
            "confidence":  result["confidence"],
            "status":      "auto_approved",
        }

    # route to human review
    pending = PendingReview(
        request_id=request_id,
        question=question,
        draft_answer=result["answer"],
        confidence=result["confidence"],
    )
    review_queue.enqueue(pending)
    asyncio.create_task(review_queue.process_next())

    try:
        await asyncio.wait_for(pending.reviewed.wait(), timeout=REVIEW_QUEUE_TIMEOUT)
        return {
            "request_id": request_id,
            "answer":     pending.reviewed_answer,
            "confidence": result["confidence"],
            "status":     "human_reviewed",
        }
    except asyncio.TimeoutError:
        return {
            "request_id": request_id,
            "answer":     (
                f"This question is under review (confidence={result['confidence']:.0%}). "
                "A verified answer will be available shortly."
            ),
            "confidence": result["confidence"],
            "status":     "pending_review",
        }


async def main() -> None:
    questions = [
        ("q001", "What is the capital of Japan?"),
        ("q002", "What is the exact net worth of a private individual?"),
        ("q003", "How do you implement binary search in Python?"),
        ("q004", "What are tomorrow's winning lottery numbers?"),
    ]

    tasks = [gated_answer(q, rid) for rid, q in questions]
    results = await asyncio.gather(*tasks)

    print("\n--- Results ---")
    for r in results:
        print(f"[{r['status']}] #{r['request_id']} conf={r['confidence']:.2f}")
        print(f"  Answer: {r['answer'][:120]}...\n")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Prevents downstream cost of correcting confidently wrong auto-answers
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Strategy | Latency | Token Overhead | Best For |
|--------|----------|---------|----------------|----------|
| 1 | Ask clarifying question | 1x | Low | Conversational agents |
| 2 | Abstain with known/unknown breakdown | 1x | Low | Risk-averse domains |
| 3 | Diversity sampling + best selection | 3x | Medium | Ambiguous, high-stakes Q&A |
| 4 | Model escalation ladder | 1–3x | Low→High | Cost-sensitive production |
| 5 | Per-field uncertainty markers | 1x | Low | Structured data extraction |
| 6 | Human review queue for low confidence | 1x (+wait) | Low | High-stakes, regulated outputs |
