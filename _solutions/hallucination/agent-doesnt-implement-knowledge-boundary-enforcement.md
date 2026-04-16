---
title: "Agent Doesn't Implement Knowledge Boundary Enforcement"
description: "Prevent agents from answering beyond their reliable knowledge—detecting when queries exceed training data, model expertise, or retrieval coverage and responding with calibrated refusals instead of hallucinations."
difficulty: intermediate
category: hallucination
tags: [hallucination, knowledge-boundary, abstention, calibration, safety]
---

## Problem

Agents answer every question with the same confident tone regardless of whether the answer falls within their training data, available context, or domain expertise. When queried outside their knowledge boundary—recent events, niche domains, specific private data—they generate plausible-sounding but incorrect answers. The fix is to detect boundary violations and respond with calibrated uncertainty rather than confident hallucination.

## Solutions

### Option 1: Explicit Boundary Declarations in System Prompt

Tell the model exactly what it knows and doesn't know, and instruct it to refuse when outside those boundaries.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class KnowledgeBoundary:
    domain: str
    known_up_to: str          # Knowledge cutoff date
    in_scope: list[str]       # Topics the agent can answer
    out_of_scope: list[str]   # Topics to refuse

    def to_system_prompt(self) -> str:
        in_scope_str = "\n".join(f"  - {t}" for t in self.in_scope)
        out_of_scope_str = "\n".join(f"  - {t}" for t in self.out_of_scope)
        return f"""You are a {self.domain} assistant with the following knowledge boundaries.

YOUR KNOWLEDGE IS CURRENT AS OF: {self.known_up_to}

TOPICS YOU CAN RELIABLY ANSWER:
{in_scope_str}

TOPICS YOU MUST DECLINE (say "I don't have reliable information on this"):
{out_of_scope_str}

RULES:
- If a question is in-scope: answer confidently and precisely.
- If a question is out-of-scope OR requires information after {self.known_up_to}: say exactly "I don't have reliable information on this. [brief reason]"
- Never guess, extrapolate, or estimate for out-of-scope topics.
- Never say "I think" or "probably" for factual claims — either know it or decline.
"""

PYTHON_BOUNDARY = KnowledgeBoundary(
    domain="Python programming",
    known_up_to="early 2024",
    in_scope=[
        "Python 3.x syntax and standard library",
        "Common third-party libraries (requests, numpy, pandas, FastAPI)",
        "Async/await patterns and asyncio",
        "Type hints and dataclasses",
        "Testing with pytest",
    ],
    out_of_scope=[
        "Python releases after early 2024",
        "Company-internal or proprietary code",
        "Real-time PyPI package availability or versions",
        "Execution results of user code",
        "Private repository contents",
    ]
)

async def bounded_answer(question: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=PYTHON_BOUNDARY.to_system_prompt(),
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

async def demo_boundary_declarations():
    test_cases = [
        ("How do I use asyncio.gather?", "IN-SCOPE"),
        ("What's new in Python 3.14?", "OUT-OF-SCOPE (future)"),
        ("What does our internal auth module do?", "OUT-OF-SCOPE (private)"),
        ("How do I write a dataclass?", "IN-SCOPE"),
        ("What is the current price of numpy on PyPI?", "OUT-OF-SCOPE (real-time)"),
    ]

    for question, expected in test_cases:
        answer = await bounded_answer(question)
        refused = "don't have reliable" in answer.lower() or "cannot" in answer.lower()
        status = "REFUSED" if refused else "ANSWERED"
        print(f"\n[{expected}] → [{status}]")
        print(f"Q: {question}")
        print(f"A: {answer.strip()[:120]}")

asyncio.run(demo_boundary_declarations())
```

### Option 2: Confidence Score Gating

Ask the model to self-assess confidence before answering, and gate responses on a minimum threshold.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CONFIDENCE_ASSESSMENT_PROMPT = """Before answering, assess your confidence in this response.

Return a JSON object with:
- "confidence": float 0.0-1.0 (your genuine confidence in accuracy)
- "reason": string (why you are or aren't confident)
- "answer": string (your answer, or empty string if declining)
- "decline": boolean (true if confidence is too low to answer reliably)

Confidence thresholds:
- 0.9+: Answer fully
- 0.7-0.9: Answer with explicit caveats
- 0.5-0.7: Provide partial information only
- <0.5: Decline and explain what you're uncertain about

Respond ONLY with the JSON object, no other text."""

async def confidence_gated_answer(question: str, min_confidence: float = 0.7) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=CONFIDENCE_ASSESSMENT_PROMPT,
        messages=[{"role": "user", "content": f"Question: {question}"}]
    )

    text = response.content[0].text.strip()
    # Extract JSON from response
    if "```" in text:
        text = text.split("```")[1].strip()
        if text.startswith("json"):
            text = text[4:].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return {
            "confidence": 0.0,
            "reason": "Failed to parse confidence assessment",
            "answer": "",
            "decline": True
        }

    # Override decline based on threshold
    if result.get("confidence", 0) < min_confidence:
        result["decline"] = True
        result["answer"] = (
            f"I'm not confident enough to answer this reliably "
            f"(confidence: {result['confidence']:.0%}). "
            f"Reason: {result['reason']}"
        )

    return result

async def demo_confidence_gating():
    questions = [
        "What is the time complexity of binary search?",
        "What did Anthropic announce last week?",
        "How does Python's GIL work?",
        "What is the exact revenue of Company XYZ in Q3 2025?",
        "What are Python decorators?",
    ]

    for q in questions:
        result = await confidence_gated_answer(q, min_confidence=0.75)
        confidence = result.get("confidence", 0)
        declined = result.get("decline", False)
        status = "DECLINED" if declined else "ANSWERED"
        print(f"\n[{status} | conf={confidence:.0%}] {q}")
        answer = result.get("answer", "")
        print(f"  {answer[:150]}")

asyncio.run(demo_confidence_gating())
```

### Option 3: Retrieval-Grounded Boundary Checking

Only answer factual questions when grounded content is available; refuse otherwise.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class RetrievalResult:
    content: str
    source: str
    relevance_score: float  # 0.0 to 1.0

# Simulated knowledge base
KNOWLEDGE_BASE = {
    "python asyncio": RetrievalResult(
        content="asyncio is Python's built-in library for writing concurrent code using async/await syntax.",
        source="python-docs-3.12",
        relevance_score=0.95
    ),
    "token bucket algorithm": RetrievalResult(
        content="A token bucket is a rate limiting algorithm where tokens accumulate at a fixed rate up to a bucket capacity.",
        source="networking-fundamentals",
        relevance_score=0.88
    ),
}

MIN_RELEVANCE_THRESHOLD = 0.7

async def retrieve(query: str) -> RetrievalResult | None:
    """Simulate semantic search — returns None if nothing relevant found."""
    query_lower = query.lower()
    for key, result in KNOWLEDGE_BASE.items():
        if any(word in query_lower for word in key.split()):
            return result
    return None

GROUNDED_SYSTEM = """You are a precise assistant. You answer questions ONLY based on the provided context.

Rules:
- If context is provided: answer using ONLY that context. Do not add external knowledge.
- If no context is provided: respond with "I don't have verified information on this topic."
- Never invent facts not present in the context.
- Quote or paraphrase context directly rather than generalizing."""

async def retrieval_grounded_answer(question: str) -> tuple[str, str]:
    """Returns (answer, source)."""
    retrieval = await retrieve(question)

    if retrieval and retrieval.relevance_score >= MIN_RELEVANCE_THRESHOLD:
        system = GROUNDED_SYSTEM
        user_message = f"""Context (source: {retrieval.source}):
{retrieval.content}

Question: {question}"""
        source = retrieval.source
    else:
        system = GROUNDED_SYSTEM
        user_message = question
        source = "none"

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text, source

async def demo_retrieval_grounded():
    questions = [
        "What is Python asyncio?",
        "How does a token bucket work?",
        "What happened in the 2025 US election?",
        "What is the speed of light?",
    ]

    for q in questions:
        answer, source = await retrieval_grounded_answer(q)
        grounded = source != "none"
        print(f"\n[{'GROUNDED' if grounded else 'REFUSED'} | src={source}]")
        print(f"Q: {q}")
        print(f"A: {answer.strip()[:150]}")

asyncio.run(demo_retrieval_grounded())
```

### Option 4: Query Classification Before Answering

Route queries through a classifier to detect out-of-boundary questions before generating an answer.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CLASSIFIER_PROMPT = """Classify whether this query can be answered reliably by a general AI assistant.

Categories:
- "factual_stable": Timeless facts (math, science, programming concepts) → CAN answer
- "factual_recent": Events/data after knowledge cutoff → CANNOT answer reliably
- "private_data": Company-internal, personal, or proprietary information → CANNOT answer
- "opinion": Subjective questions → CAN answer with caveat
- "real_time": Current prices, live data, today's news → CANNOT answer
- "procedural": How-to questions on established topics → CAN answer

Return JSON: {"category": "...", "can_answer": true/false, "reason": "..."}
Respond ONLY with JSON."""

ANSWER_SYSTEM = "You are a precise assistant. Answer concisely and accurately."
DECLINE_TEMPLATE = (
    "I can't reliably answer this. {reason}. "
    "For accurate information, please check {suggestion}."
)

SUGGESTIONS = {
    "factual_recent": "current news sources or official announcements",
    "private_data": "your internal documentation or team members",
    "real_time": "live data sources or APIs",
}

async def classify_query(question: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=CLASSIFIER_PROMPT,
        messages=[{"role": "user", "content": f"Query: {question}"}]
    )
    text = response.content[0].text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"category": "unknown", "can_answer": False, "reason": "classification failed"}

async def boundary_enforced_answer(question: str) -> str:
    classification = await classify_query(question)

    if not classification.get("can_answer", False):
        category = classification.get("category", "unknown")
        reason = classification.get("reason", "outside knowledge boundary")
        suggestion = SUGGESTIONS.get(category, "authoritative sources")
        return DECLINE_TEMPLATE.format(reason=reason, suggestion=suggestion)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250,
        system=ANSWER_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

async def demo_query_classification():
    questions = [
        "What is Big O notation?",
        "What is the current stock price of Anthropic?",
        "What did our CEO say in yesterday's all-hands?",
        "How do I implement a binary search tree in Python?",
        "Who won the 2026 World Cup?",
    ]

    for q in questions:
        answer = await boundary_enforced_answer(q)
        declined = "can't reliably" in answer or "cannot" in answer.lower()
        print(f"\n[{'DECLINED' if declined else 'ANSWERED'}] {q}")
        print(f"  {answer.strip()[:150]}")

asyncio.run(demo_query_classification())
```

### Option 5: Multi-Layer Boundary Stack

Combine date-cutoff detection, topic classification, and confidence gating in a pipeline.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from enum import Enum

client = AsyncAnthropic()

class BoundaryViolation(Enum):
    DATE_CUTOFF = "date_cutoff"
    OUT_OF_DOMAIN = "out_of_domain"
    PRIVATE_DATA = "private_data"
    REAL_TIME = "real_time"
    NONE = "none"

@dataclass
class BoundaryCheck:
    violation: BoundaryViolation
    confidence: float
    detail: str

DATE_PATTERNS = [
    r"\b(202[5-9]|203\d)\b",           # Future years
    r"\b(last|this|next)\s+(week|month|year)\b",
    r"\b(today|yesterday|tomorrow)\b",
    r"\brecently\b",
    r"\blast\s+\w+\s+announced\b",
]

PRIVATE_PATTERNS = [
    r"\b(our|your|my)\s+(company|team|codebase|repo|database|system|client)\b",
    r"\b(internal|proprietary|confidential)\b",
    r"\b(our CEO|our CTO|our VP)\b",
]

REAL_TIME_PATTERNS = [
    r"\b(current|live|real.?time)\s+(price|data|stock|rate)\b",
    r"\btoday'?s\s+(price|rate|weather|news)\b",
    r"\bright now\b",
]

def pattern_check(text: str) -> BoundaryCheck:
    text_lower = text.lower()

    for pattern in DATE_PATTERNS:
        if re.search(pattern, text_lower):
            return BoundaryCheck(
                violation=BoundaryViolation.DATE_CUTOFF,
                confidence=0.85,
                detail="Query appears to reference events after knowledge cutoff"
            )

    for pattern in PRIVATE_PATTERNS:
        if re.search(pattern, text_lower):
            return BoundaryCheck(
                violation=BoundaryViolation.PRIVATE_DATA,
                confidence=0.90,
                detail="Query references private or proprietary information"
            )

    for pattern in REAL_TIME_PATTERNS:
        if re.search(pattern, text_lower):
            return BoundaryCheck(
                violation=BoundaryViolation.REAL_TIME,
                confidence=0.92,
                detail="Query requires real-time data"
            )

    return BoundaryCheck(violation=BoundaryViolation.NONE, confidence=1.0, detail="")

VIOLATION_RESPONSES = {
    BoundaryViolation.DATE_CUTOFF: (
        "My training data has a cutoff date, so I can't reliably answer questions about "
        "recent events. Please check current news sources."
    ),
    BoundaryViolation.PRIVATE_DATA: (
        "I don't have access to private, internal, or proprietary information. "
        "Please consult your internal documentation or team members."
    ),
    BoundaryViolation.REAL_TIME: (
        "I can't access real-time data. For current information, please use a live data source."
    ),
}

async def multi_layer_answer(question: str) -> tuple[str, BoundaryViolation]:
    # Layer 1: Fast pattern matching
    check = pattern_check(question)
    if check.violation != BoundaryViolation.NONE and check.confidence >= 0.8:
        return VIOLATION_RESPONSES[check.violation], check.violation

    # Layer 2: Model-based answer with implicit boundary awareness
    system = (
        "Answer only what you know with high confidence. "
        "If you lack reliable information, say 'I don't have reliable information on this.' "
        "Do not speculate or guess."
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text, BoundaryViolation.NONE

async def demo_multi_layer():
    questions = [
        "How does quicksort work?",
        "What's happening in the news today?",
        "What's in our internal auth service?",
        "What is the current EUR/USD rate?",
        "Who was Turing?",
        "What did Anthropic announce this week?",
    ]

    for q in questions:
        answer, violation = await multi_layer_answer(q)
        tag = violation.value if violation != BoundaryViolation.NONE else "answered"
        print(f"\n[{tag}] {q}")
        print(f"  {answer.strip()[:150]}")

asyncio.run(demo_multi_layer())
```

### Option 6: Graceful Partial-Knowledge Responses

When query partially exceeds boundaries, answer the in-scope portion and explicitly decline the rest.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

PARTIAL_BOUNDARY_SYSTEM = """You are a precise assistant with explicit knowledge boundaries.

When answering questions:
1. IDENTIFY which parts of the question you can answer reliably
2. ANSWER those parts fully
3. EXPLICITLY DECLINE the parts you cannot answer
4. SUGGEST where the user can find the out-of-scope information

Format your response as:
✓ [What I can tell you]: ...
✗ [What I cannot tell you]: ...
→ [Where to find it]: ...

Only use sections that apply. If you can answer everything, just answer normally.
If you cannot answer anything, just say what you cannot answer and where to look."""

async def partial_knowledge_response(question: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=PARTIAL_BOUNDARY_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

async def demo_partial_knowledge():
    questions = [
        # Mixed: has in-scope and out-of-scope components
        "How does HTTPS work, and what were the latest TLS vulnerabilities discovered this month?",
        "Explain Python's async/await and tell me about our team's current sprint tickets.",
        "What is the CAP theorem and what is your company's database architecture?",
        # Fully in-scope
        "What is a deadlock in concurrent programming?",
        # Fully out-of-scope
        "What did our product team ship last Tuesday?",
    ]

    for q in questions:
        print(f"\nQ: {q}")
        answer = await partial_knowledge_response(q)
        print(f"A: {answer.strip()}")
        print("-" * 60)

asyncio.run(demo_partial_knowledge())
```

## Comparison

| Approach | Detection Method | False Positive Risk | Latency Added | Best For |
|---|---|---|---|---|
| Boundary Declarations | Prompt-based | Medium | None | Domain-specific agents |
| Confidence Score Gating | Self-assessment | Medium | 1 extra LLM call | General-purpose agents |
| Retrieval-Grounded | Context presence | Low | Retrieval latency | RAG-based agents |
| Query Classification | Pre-answer routing | Low-Medium | 1 extra LLM call | High-stakes domains |
| Multi-Layer Stack | Pattern + LLM | Low | Minimal | Production agents |
| Partial-Knowledge Responses | LLM segmentation | Low | None | Mixed-scope queries |

**Choose Boundary Declarations** as the quickest win—add a clear do/don't list to your system prompt and most out-of-scope hallucinations disappear immediately. **Choose Multi-Layer Stack** for production agents where both false positives (unnecessary refusals) and false negatives (hallucinations) are costly. **Choose Partial-Knowledge Responses** when user queries often span both known and unknown territory—partial answers are more useful than blanket refusals.
