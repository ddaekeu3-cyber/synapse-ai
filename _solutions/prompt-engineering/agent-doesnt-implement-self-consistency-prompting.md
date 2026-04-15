---
layout: solution
title: "Agent Doesn't Implement Self-Consistency Prompting"
category: prompt-engineering
description: "Agents that generate a single response for complex reasoning tasks are unreliable — one wrong chain-of-thought leads to a wrong answer. Self-consistency samples multiple independent reasoning paths and selects the most frequent or highest-confidence answer, dramatically improving accuracy on math, logic, and multi-step tasks."
tags: [self-consistency, chain-of-thought, reasoning, voting, ensemble, accuracy, hallucination]
---

# Agent Doesn't Implement Self-Consistency Prompting

## Problem

When an agent generates a single response to a complex reasoning question, a single wrong step in the chain of thought produces a wrong final answer. The model has no way to self-correct because it commits to the first path it takes. Self-consistency prompting — introduced in Wang et al. 2022 — generates multiple independent reasoning paths with non-zero temperature, then selects the answer that appears most frequently across all paths. On math and logical reasoning benchmarks, this increases accuracy by 10–20% at the cost of 3–10x more API calls.

**Symptoms:**
- Answers to arithmetic/logic questions vary across runs
- Agent confidently gives wrong answers on multi-step problems
- No way to detect when the model is uncertain about a reasoning chain
- Single-shot answers fail on tasks requiring careful deduction

---

## Option 1: Basic Majority Vote Across N Samples

```python
import anthropic
import re
from collections import Counter

def extract_final_answer(text: str) -> str:
    """Extract the answer after 'Therefore', 'The answer is', or the last number."""
    patterns = [
        r"(?:the answer is|therefore[,:]?|final answer[:]?|result[:]?)\s*([^\n.]+)",
        r"=\s*([\d,]+(?:\.\d+)?)\s*$",
        r"([\d,]+(?:\.\d+)?)\s*$"
    ]
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower, re.MULTILINE)
        if match:
            return match.group(1).strip().replace(",", "")
    # Last resort: extract all numbers and return the last one
    numbers = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    return numbers[-1] if numbers else text.strip()[-50:]

def self_consistency_vote(
    question: str,
    n_samples: int = 5,
    temperature: float = 0.7
) -> dict:
    client = anthropic.Anthropic()

    system_prompt = """You are a careful reasoning assistant.
For each problem, think step-by-step before giving your final answer.
Always end with: "The answer is: [your answer]"
"""

    answers = []
    reasoning_paths = []

    for i in range(n_samples):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": question}]
        )
        text = response.content[0].text
        answer = extract_final_answer(text)
        answers.append(answer)
        reasoning_paths.append(text)
        print(f"  Sample {i+1}: answer={answer!r}")

    # Majority vote
    vote_counts = Counter(answers)
    winner, winner_votes = vote_counts.most_common(1)[0]
    confidence = winner_votes / n_samples

    return {
        "question": question,
        "samples": n_samples,
        "final_answer": winner,
        "confidence": confidence,
        "vote_distribution": dict(vote_counts),
        "all_answers": answers,
        "reasoning_paths": reasoning_paths
    }

# Test on a math problem
result = self_consistency_vote(
    question="A store sells apples for $0.75 each and oranges for $1.25 each. "
             "If Sarah buys 4 apples and 3 oranges, and pays with a $10 bill, "
             "how much change does she receive?",
    n_samples=5,
    temperature=0.7
)

print(f"\nFinal answer: {result['final_answer']}")
print(f"Confidence: {result['confidence']:.0%}")
print(f"Vote distribution: {result['vote_distribution']}")

# Expected Token Savings: -400% (5x more tokens than single call) — accuracy improvement trade-off
# Environment: Best for complex math/logic where accuracy matters more than cost
```

---

## Option 2: Parallel Sampling with Async for Speed

```python
import anthropic
import asyncio
import re
from collections import Counter
from dataclasses import dataclass

@dataclass
class ReasoningPath:
    path_id: int
    full_text: str
    extracted_answer: str
    token_count: int

def extract_answer(text: str) -> str:
    patterns = [
        r"the answer is[:\s]+([^\n.]+)",
        r"therefore[,:\s]+([^\n.]+)",
        r"result[:\s]+([\d,.$%]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text.lower())
        if m:
            val = m.group(1).strip()
            # Normalize numbers
            val = re.sub(r"[,$]", "", val)
            return val.split(".")[0].strip()
    # Fallback: last numeric token
    nums = re.findall(r"\$?[\d,]+(?:\.\d{1,2})?", text)
    return nums[-1].replace("$", "").replace(",", "") if nums else text[-30:].strip()

async def sample_path(
    client: anthropic.AsyncAnthropic,
    question: str,
    path_id: int,
    temperature: float
) -> ReasoningPath:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        temperature=temperature,
        system="Solve step by step. End with 'The answer is: [answer]'",
        messages=[{"role": "user", "content": question}]
    )
    text = response.content[0].text
    return ReasoningPath(
        path_id=path_id,
        full_text=text,
        extracted_answer=extract_answer(text),
        token_count=response.usage.input_tokens + response.usage.output_tokens
    )

async def parallel_self_consistency(
    question: str,
    n_samples: int = 7,
    temperature: float = 0.8
) -> dict:
    client = anthropic.AsyncAnthropic()

    # Sample all paths in parallel
    tasks = [
        sample_path(client, question, i, temperature)
        for i in range(n_samples)
    ]
    paths = await asyncio.gather(*tasks)

    answers = [p.extracted_answer for p in paths]
    vote_counts = Counter(answers)
    winner, winner_votes = vote_counts.most_common(1)[0]

    # Find the best reasoning path for the winning answer
    winning_paths = [p for p in paths if p.extracted_answer == winner]
    representative = winning_paths[0]

    total_tokens = sum(p.token_count for p in paths)
    print(f"\nParallel sampling complete ({n_samples} paths):")
    for p in paths:
        marker = "✓" if p.extracted_answer == winner else "✗"
        print(f"  [{marker}] Path {p.path_id}: {p.extracted_answer!r}")

    return {
        "final_answer": winner,
        "confidence": winner_votes / n_samples,
        "vote_distribution": dict(vote_counts),
        "total_tokens_used": total_tokens,
        "representative_reasoning": representative.full_text,
        "all_paths": paths
    }

result = asyncio.run(parallel_self_consistency(
    question="A train travels at 60 mph for the first half of a journey "
             "and 40 mph for the second half. What is the average speed for the entire trip?",
    n_samples=7,
    temperature=0.8
))

print(f"\nAnswer: {result['final_answer']} (confidence: {result['confidence']:.0%})")
print(f"Total tokens: {result['total_tokens_used']}")
print(f"\nRepresentative reasoning:\n{result['representative_reasoning'][:300]}...")

# Expected Token Savings: -600% vs single call; parallel execution reduces wall-clock time by ~N/1
# Environment: asyncio required; ideal when latency matters more than token cost
```

---

## Option 3: Adaptive Sampling — Stop Early When Confident

```python
import anthropic
import re
from collections import Counter
import math

def extract_numeric_answer(text: str) -> str:
    # Try "The answer is X" pattern
    m = re.search(r"(?:the answer is|=)\s*\$?([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        return m.group(1).replace(",", "")
    # Try last number in text
    nums = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    return nums[-1] if nums else ""

def plurality_confidence(votes: list[str]) -> tuple[str, float]:
    """Return (winner, confidence) from votes."""
    if not votes:
        return "", 0.0
    counts = Counter(votes)
    winner, top_count = counts.most_common(1)[0]
    return winner, top_count / len(votes)

def adaptive_self_consistency(
    question: str,
    min_samples: int = 3,
    max_samples: int = 10,
    confidence_threshold: float = 0.8,
    temperature: float = 0.7
) -> dict:
    """Sample until confident or max_samples reached."""
    client = anthropic.Anthropic()
    votes = []
    iterations = 0
    stopped_early = False

    system = "Think carefully step by step. End with 'The answer is: [number]'"

    for i in range(max_samples):
        iterations += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": question}]
        )
        answer = extract_numeric_answer(response.content[0].text)
        votes.append(answer)

        winner, confidence = plurality_confidence(votes)
        print(f"  Sample {i+1}: answer={answer!r}, current winner={winner!r} ({confidence:.0%})")

        # Early stopping: only after min_samples, if confidence is high enough
        if i + 1 >= min_samples and confidence >= confidence_threshold:
            stopped_early = True
            print(f"  Early stop at sample {i+1}: confidence {confidence:.0%} >= {confidence_threshold:.0%}")
            break

    winner, confidence = plurality_confidence(votes)
    return {
        "final_answer": winner,
        "confidence": confidence,
        "samples_used": iterations,
        "max_samples": max_samples,
        "stopped_early": stopped_early,
        "vote_distribution": dict(Counter(votes))
    }

# Example: geometric problem
result = adaptive_self_consistency(
    question="A rectangle has a perimeter of 56 cm. If its length is 4 cm more than its width, "
             "what is the area of the rectangle?",
    min_samples=3,
    max_samples=10,
    confidence_threshold=0.8
)

print(f"\nResult: {result['final_answer']} cm²")
print(f"Used {result['samples_used']}/{result['max_samples']} samples")
print(f"Confidence: {result['confidence']:.0%}")
print(f"Early stop: {result['stopped_early']}")

# Expected Token Savings: ~40-60% vs fixed-N sampling — stops as soon as model agrees with itself
# Environment: Ideal for interactive use where you want accuracy but want to minimize cost
```

---

## Option 4: Self-Consistency with Structured Output Parsing

```python
import anthropic
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

@dataclass
class StructuredAnswer:
    reasoning_steps: list[str]
    final_answer: str
    answer_type: str  # "numeric", "boolean", "choice", "text"
    confidence_expressed: Optional[str]  # e.g. "certain", "likely", "unsure"

def parse_structured_answer(text: str) -> StructuredAnswer:
    """Parse structured JSON response from the model."""
    # Try to extract JSON block
    json_match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            return StructuredAnswer(
                reasoning_steps=data.get("steps", []),
                final_answer=str(data.get("answer", "")),
                answer_type=data.get("type", "text"),
                confidence_expressed=data.get("confidence", None)
            )
        except json.JSONDecodeError:
            pass

    # Fallback: extract answer field
    m = re.search(r'"answer"\s*:\s*"?([^",\n}]+)"?', text)
    answer = m.group(1).strip() if m else text[-50:].strip()
    return StructuredAnswer(
        reasoning_steps=[],
        final_answer=answer,
        answer_type="text",
        confidence_expressed=None
    )

def structured_self_consistency(question: str, n_samples: int = 5) -> dict:
    client = anthropic.Anthropic()

    system = """You are a precise reasoning assistant. Respond ONLY with valid JSON in this format:
```json
{
  "steps": ["step 1 reasoning", "step 2 reasoning", "..."],
  "answer": "your final answer",
  "type": "numeric|boolean|choice|text",
  "confidence": "certain|likely|unsure"
}
```"""

    parsed_answers: list[StructuredAnswer] = []

    for i in range(n_samples):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            temperature=0.7,
            system=system,
            messages=[{"role": "user", "content": question}]
        )
        parsed = parse_structured_answer(response.content[0].text)
        parsed_answers.append(parsed)
        print(f"  Sample {i+1}: answer={parsed.final_answer!r}, confidence={parsed.confidence_expressed}")

    # Vote on final answer
    all_answers = [a.final_answer.lower().strip() for a in parsed_answers]
    vote_counts = Counter(all_answers)
    winner_raw, winner_votes = vote_counts.most_common(1)[0]

    # Find original casing/formatting from winner
    winner_original = next(
        a.final_answer for a in parsed_answers
        if a.final_answer.lower().strip() == winner_raw
    )

    # Find best reasoning path (most steps, from winner answer)
    winning_answers = [a for a in parsed_answers if a.final_answer.lower().strip() == winner_raw]
    best_reasoning = max(winning_answers, key=lambda a: len(a.reasoning_steps))

    # Confidence meta-analysis
    confidence_votes = Counter(a.confidence_expressed for a in winning_answers if a.confidence_expressed)

    return {
        "final_answer": winner_original,
        "vote_share": winner_votes / n_samples,
        "vote_distribution": dict(vote_counts),
        "best_reasoning_steps": best_reasoning.reasoning_steps,
        "model_confidence": confidence_votes.most_common(1)[0][0] if confidence_votes else "unknown",
        "answer_type": parsed_answers[0].answer_type
    }

result = structured_self_consistency(
    question="Is 1729 a prime number? If not, what are its prime factors?",
    n_samples=5
)
print(f"\nAnswer: {result['final_answer']}")
print(f"Vote share: {result['vote_share']:.0%}")
print(f"Answer type: {result['answer_type']}")
print(f"Model confidence: {result['model_confidence']}")
print(f"Reasoning steps:")
for step in result['best_reasoning_steps']:
    print(f"  - {step}")

# Expected Token Savings: -500% vs single call; structured output makes answer extraction reliable
# Environment: Works best for factual/logical questions with deterministic correct answers
```

---

## Option 5: Self-Consistency with Semantic Answer Clustering

```python
import anthropic
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

@dataclass
class AnswerCluster:
    canonical_answer: str
    member_answers: list[str]
    vote_count: int
    sample_reasoning: str

def normalize_answer(text: str) -> str:
    """Normalize answer for semantic comparison."""
    # Remove common prefixes
    text = re.sub(r"^(?:the answer is|therefore|result)[:\s]+", "", text.strip(), flags=re.IGNORECASE)
    # Remove trailing punctuation
    text = text.rstrip(".,;:!?")
    # Normalize numbers: remove $ , spacing
    text = re.sub(r"[$,]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()

def are_semantically_equal(a: str, b: str) -> bool:
    """Check if two answers are semantically equivalent."""
    a_norm = normalize_answer(a)
    b_norm = normalize_answer(b)
    if a_norm == b_norm:
        return True
    # Try numeric comparison
    try:
        return abs(float(a_norm) - float(b_norm)) < 0.01
    except (ValueError, TypeError):
        pass
    # Check if one contains the other (for multi-word answers)
    return a_norm in b_norm or b_norm in a_norm

def cluster_answers(answers_with_reasoning: list[tuple[str, str]]) -> list[AnswerCluster]:
    """Group semantically equivalent answers into clusters."""
    clusters: list[AnswerCluster] = []

    for answer, reasoning in answers_with_reasoning:
        placed = False
        for cluster in clusters:
            if are_semantically_equal(answer, cluster.canonical_answer):
                cluster.member_answers.append(answer)
                cluster.vote_count += 1
                placed = True
                break
        if not placed:
            clusters.append(AnswerCluster(
                canonical_answer=normalize_answer(answer),
                member_answers=[answer],
                vote_count=1,
                sample_reasoning=reasoning
            ))

    return sorted(clusters, key=lambda c: c.vote_count, reverse=True)

def semantic_self_consistency(question: str, n_samples: int = 6) -> dict:
    client = anthropic.Anthropic()

    system = "Reason step by step. State your final answer clearly at the end."
    answers_with_reasoning = []

    for i in range(n_samples):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            temperature=0.75,
            system=system,
            messages=[{"role": "user", "content": question}]
        )
        text = response.content[0].text
        # Extract the last sentence as the answer
        sentences = [s.strip() for s in text.split(".") if s.strip()]
        raw_answer = sentences[-1] if sentences else text[-60:]
        answers_with_reasoning.append((raw_answer, text))
        print(f"  Sample {i+1}: {raw_answer[:60]!r}")

    clusters = cluster_answers(answers_with_reasoning)
    top_cluster = clusters[0]

    print(f"\nClusters found: {len(clusters)}")
    for c in clusters:
        print(f"  [{c.vote_count} votes] {c.canonical_answer!r}")

    return {
        "final_answer": top_cluster.canonical_answer,
        "winning_votes": top_cluster.vote_count,
        "total_samples": n_samples,
        "confidence": top_cluster.vote_count / n_samples,
        "cluster_count": len(clusters),
        "is_unanimous": len(clusters) == 1,
        "best_reasoning": top_cluster.sample_reasoning
    }

# Test on a word problem
result = semantic_self_consistency(
    question="Three friends split a restaurant bill equally. The bill was $87.60 "
             "and they want to leave a 20% tip. How much does each person pay in total?",
    n_samples=6
)
print(f"\nFinal answer: {result['final_answer']}")
print(f"Confidence: {result['confidence']:.0%} ({result['winning_votes']}/{result['total_samples']} votes)")
print(f"Unanimous: {result['is_unanimous']}")

# Expected Token Savings: -500% vs single; semantic clustering handles formatting variants
# Environment: Useful when answers may be phrased differently but mean the same thing
```

---

## Option 6: Self-Consistency Caching — Reuse Paths for Similar Questions

```python
import anthropic
import hashlib
import json
import re
import time
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

@dataclass
class CachedConsensus:
    question_hash: str
    question: str
    final_answer: str
    confidence: float
    n_samples: int
    vote_distribution: dict
    cached_at: float = field(default_factory=time.time)
    hits: int = 0

class ConsensusCache:
    def __init__(self, db_path: str = "/tmp/consensus_cache.db", ttl_seconds: int = 3600):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.ttl = ttl_seconds
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS consensus_cache (
                question_hash TEXT PRIMARY KEY,
                question TEXT,
                final_answer TEXT,
                confidence REAL,
                n_samples INTEGER,
                vote_distribution TEXT,
                cached_at REAL,
                hits INTEGER DEFAULT 0
            )
        """)
        self.db.commit()

    def _hash_question(self, question: str) -> str:
        normalized = re.sub(r"\s+", " ", question.strip().lower())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def get(self, question: str) -> CachedConsensus | None:
        q_hash = self._hash_question(question)
        row = self.db.execute(
            "SELECT * FROM consensus_cache WHERE question_hash = ?", (q_hash,)
        ).fetchone()
        if row is None:
            return None
        cached_at = row[6]
        if time.time() - cached_at > self.ttl:
            self.db.execute("DELETE FROM consensus_cache WHERE question_hash = ?", (q_hash,))
            self.db.commit()
            return None
        # Update hit count
        self.db.execute(
            "UPDATE consensus_cache SET hits = hits + 1 WHERE question_hash = ?", (q_hash,)
        )
        self.db.commit()
        return CachedConsensus(
            question_hash=row[0], question=row[1], final_answer=row[2],
            confidence=row[3], n_samples=row[4],
            vote_distribution=json.loads(row[5]), cached_at=row[6], hits=row[7] + 1
        )

    def put(self, question: str, answer: str, confidence: float,
            n_samples: int, vote_dist: dict):
        q_hash = self._hash_question(question)
        self.db.execute("""
            INSERT OR REPLACE INTO consensus_cache
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (q_hash, question, answer, confidence, n_samples,
              json.dumps(vote_dist), time.time()))
        self.db.commit()

def extract_final_answer(text: str) -> str:
    m = re.search(r"(?:the answer is|=|result:?)\s*\$?([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        return m.group(1).replace(",", "")
    nums = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    return nums[-1] if nums else text.strip()[-40:]

def cached_self_consistency(
    question: str,
    n_samples: int = 5,
    confidence_threshold: float = 0.6,
    cache: ConsensusCache = None
) -> dict:
    if cache is None:
        cache = ConsensusCache()

    # Check cache first
    cached = cache.get(question)
    if cached:
        print(f"[Cache HIT] Returning cached answer (hits={cached.hits})")
        return {
            "final_answer": cached.final_answer,
            "confidence": cached.confidence,
            "samples_used": 0,
            "from_cache": True,
            "cache_hits": cached.hits
        }

    print(f"[Cache MISS] Running {n_samples} samples...")
    client = anthropic.Anthropic()
    votes = []

    for i in range(n_samples):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            temperature=0.7,
            system="Think step by step and give the final numerical answer.",
            messages=[{"role": "user", "content": question}]
        )
        answer = extract_final_answer(response.content[0].text)
        votes.append(answer)
        print(f"  Sample {i+1}: {answer!r}")

    vote_counts = Counter(votes)
    winner, winner_votes = vote_counts.most_common(1)[0]
    confidence = winner_votes / n_samples

    # Cache if confidence meets threshold
    if confidence >= confidence_threshold:
        cache.put(question, winner, confidence, n_samples, dict(vote_counts))
        print(f"[Cache STORE] Confidence {confidence:.0%} >= {confidence_threshold:.0%}")
    else:
        print(f"[Cache SKIP] Confidence {confidence:.0%} < {confidence_threshold:.0%} (too uncertain)")

    return {
        "final_answer": winner,
        "confidence": confidence,
        "samples_used": n_samples,
        "from_cache": False,
        "vote_distribution": dict(vote_counts)
    }

cache = ConsensusCache(ttl_seconds=300)
question = "If a car depreciates by 15% per year and is currently worth $24,000, what will it be worth after 3 years? (Round to nearest dollar)"

# First call: samples
result1 = cached_self_consistency(question, n_samples=5, cache=cache)
print(f"\nFirst call: {result1['final_answer']} (confidence={result1['confidence']:.0%}, from_cache={result1['from_cache']})")

# Second call: cache hit
result2 = cached_self_consistency(question, n_samples=5, cache=cache)
print(f"Second call: {result2['final_answer']} (samples_used={result2['samples_used']}, from_cache={result2['from_cache']})")

# Expected Token Savings: ~80% on repeated questions — subsequent calls are free
# Environment: SQLite cache; replace with Redis for multi-process or distributed deployments
```

---

## Comparison

| Option | Samples | Stop Early | Answer Parsing | Caching | Best For |
|--------|---------|-----------|---------------|---------|----------|
| Majority Vote | Fixed N | No | Regex | No | Baseline accuracy improvement |
| Parallel Async | Fixed N | No | Regex | No | Low latency (parallel calls) |
| Adaptive Sampling | Min–Max | Yes | Regex | No | Cost-sensitive accuracy |
| Structured Output | Fixed N | No | JSON | No | Reliable parsing of complex answers |
| Semantic Clustering | Fixed N | No | Semantic | No | Natural language / multi-phrasing answers |
| Cache + Voting | Fixed N | No | Regex | SQLite | Repeated similar questions |

**Recommendation:** Use **Option 3** (adaptive sampling) as the default — it stops early when the model is confident, saving tokens while still achieving consensus accuracy. Use **Option 2** (parallel async) when you need low latency. Add **Option 6** caching on top of any approach when the same questions are asked repeatedly.
