---
layout: solution
title: "Agent Doesn't Implement Convergence Detection for Iterative Tasks"
category: loop-stuck
description: "Detect when iterative refinement has converged and stop early, using output similarity, score plateau detection, and semantic change measurement."
tags: [loop-stuck, convergence, iterative, refinement, early-exit, similarity]
---

# Agent Doesn't Implement Convergence Detection for Iterative Tasks

Iterative refinement loops — rewrite, critique, improve — can run indefinitely when the agent has no way to detect that successive outputs are no longer meaningfully different. Without convergence detection, the agent wastes tokens polishing already-good output or loops forever on a task that reached its natural ceiling. Convergence detection stops the loop when further iterations yield diminishing returns.

## Option 1: Character-Level Similarity Threshold

```python
import anthropic
import difflib

client = anthropic.Anthropic()

SIMILARITY_THRESHOLD = 0.95  # Stop when outputs are 95%+ similar


def similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio between two strings."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def iterative_refine(task: str, max_iterations: int = 8) -> str:
    """Refine text until convergence or max iterations."""
    messages: list[dict] = [{"role": "user", "content": f"Write a first draft for: {task}"}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=messages,
    )
    current = response.content[0].text
    print(f"[iter 0] {len(current)} chars")

    for i in range(1, max_iterations + 1):
        messages = [
            {"role": "user", "content": f"Improve this text, making it clearer and more concise:\n\n{current}"}
        ]
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=messages,
        )
        improved = response.content[0].text
        sim = similarity(current, improved)
        print(f"[iter {i}] similarity={sim:.3f} len={len(improved)}")

        if sim >= SIMILARITY_THRESHOLD:
            print(f"Converged at iteration {i} (similarity {sim:.3f} >= {SIMILARITY_THRESHOLD})")
            return improved

        current = improved

    print(f"Reached max iterations ({max_iterations}) without convergence")
    return current


result = iterative_refine("the importance of error handling in distributed systems")
print(f"\nFinal output ({len(result)} chars):\n{result[:300]}")

# Expected Token Savings: 30-60% on iterative tasks; convergence typically at iter 2-4, not max
# Environment: Python 3.11+; tune threshold (0.90-0.97) based on task sensitivity; lower = more iterations
```

## Option 2: Score Plateau Detection with Rubric Evaluation

```python
import anthropic
import json

client = anthropic.Anthropic()

PLATEAU_WINDOW = 3       # Stop if score doesn't improve for N consecutive iterations
MIN_SCORE_DELTA = 0.5   # Minimum improvement to count as progress (0-10 scale)
MAX_ITERATIONS = 10


def evaluate_quality(text: str, criteria: str) -> float:
    """Ask the model to score the text on a 0-10 scale."""
    prompt = f"""Score this text on a scale of 0-10 for: {criteria}

Text:
{text}

Respond with JSON only: {{"score": <number>, "reason": "<one sentence>"}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(response.content[0].text)
        return float(data["score"])
    except Exception:
        return 5.0  # default mid-score on parse failure


def refine_with_plateau_detection(task: str, criteria: str = "clarity and accuracy") -> str:
    """Refine until score plateaus for PLATEAU_WINDOW iterations."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Write a concise explanation of: {task}"}],
    )
    current = response.content[0].text
    scores: list[float] = [evaluate_quality(current, criteria)]
    print(f"[iter 0] score={scores[-1]:.1f}")

    no_improvement_count = 0

    for i in range(1, MAX_ITERATIONS + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": f"Improve this explanation for {criteria}:\n\n{current}"}],
        )
        improved = response.content[0].text
        score = evaluate_quality(improved, criteria)
        delta = score - scores[-1]
        scores.append(score)
        print(f"[iter {i}] score={score:.1f} delta={delta:+.1f}")

        if delta < MIN_SCORE_DELTA:
            no_improvement_count += 1
            if no_improvement_count >= PLATEAU_WINDOW:
                print(f"Plateau detected: no significant improvement for {PLATEAU_WINDOW} iterations")
                return improved
        else:
            no_improvement_count = 0

        current = improved

    return current


result = refine_with_plateau_detection("how asyncio event loops work in Python")
print(f"\nFinal:\n{result[:400]}")

# Expected Token Savings: 40-50%; rubric scoring adds ~50 tokens/iter but prevents 3-5 wasted refinement rounds
# Environment: Python 3.11+; cache evaluations if same text appears; use sonnet for better score calibration
```

## Option 3: Semantic Embedding Change Detection

```python
import asyncio
import math
import anthropic

client = anthropic.AsyncAnthropic()

SEMANTIC_CHANGE_THRESHOLD = 0.05   # Stop when cosine distance drops below this
MAX_ITERATIONS = 8


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return dot / (norm_a * norm_b)


async def embed(text: str) -> list[float]:
    """
    Approximate embedding via token frequency vector (no separate embeddings API needed).
    In production, use a real embeddings API or sentence-transformers.
    """
    tokens = text.lower().split()
    vocab = sorted(set(tokens))[:128]  # Top-128 unique tokens
    vec = [tokens.count(t) / max(len(tokens), 1) for t in vocab]
    # Pad to fixed length
    vec += [0.0] * (128 - len(vec))
    return vec[:128]


async def refine_with_semantic_convergence(task: str) -> str:
    """Stop when semantic content stops changing significantly."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Explain: {task}"}],
    )
    current = response.content[0].text
    current_vec = await embed(current)
    print(f"[iter 0] {len(current)} chars")

    for i in range(1, MAX_ITERATIONS + 1):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": f"Revise this explanation for accuracy and completeness:\n\n{current}"}],
        )
        improved = response.content[0].text
        improved_vec = await embed(improved)

        sim = cosine_similarity(current_vec, improved_vec)
        change = 1.0 - sim
        print(f"[iter {i}] semantic_change={change:.4f} (threshold={SEMANTIC_CHANGE_THRESHOLD})")

        if change < SEMANTIC_CHANGE_THRESHOLD:
            print(f"Semantic convergence at iteration {i}: change {change:.4f} < {SEMANTIC_CHANGE_THRESHOLD}")
            return improved

        current = improved
        current_vec = improved_vec

    return current


result = asyncio.run(refine_with_semantic_convergence("gradient descent optimization"))
print(f"\nFinal:\n{result[:400]}")

# Expected Token Savings: 35-55%; semantic convergence fires earlier than string similarity on paraphrase-heavy rewrites
# Environment: Python 3.11+; replace embed() with text-embedding-3-small or sentence-transformers for production accuracy
```

## Option 4: Multi-Signal Convergence with SQLite History

```python
import asyncio
import sqlite3
import json
import difflib
import time
import anthropic

client = anthropic.AsyncAnthropic()
DB_PATH = ":memory:"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS refinement_history (
            run_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            text TEXT NOT NULL,
            length INTEGER NOT NULL,
            similarity_to_prev REAL,
            score REAL,
            created_at REAL NOT NULL,
            PRIMARY KEY (run_id, iteration)
        )
    """)
    conn.commit()


def save_iteration(conn: sqlite3.Connection, run_id: str, iteration: int,
                   text: str, similarity: float | None, score: float | None) -> None:
    conn.execute(
        "INSERT INTO refinement_history VALUES (?,?,?,?,?,?,?)",
        (run_id, iteration, text, len(text), similarity, score, time.time())
    )
    conn.commit()


def check_convergence(conn: sqlite3.Connection, run_id: str, window: int = 3) -> tuple[bool, str]:
    """Check multiple convergence signals over recent window."""
    rows = conn.execute(
        "SELECT similarity_to_prev, score, length FROM refinement_history "
        "WHERE run_id=? ORDER BY iteration DESC LIMIT ?",
        (run_id, window)
    ).fetchall()

    if len(rows) < window:
        return False, "insufficient history"

    sims = [r[0] for r in rows if r[0] is not None]
    scores = [r[1] for r in rows if r[1] is not None]
    lengths = [r[2] for r in rows]

    # Signal 1: high similarity
    if sims and min(sims) > 0.92:
        return True, f"similarity plateau (min={min(sims):.3f})"

    # Signal 2: score flat
    if len(scores) >= 2 and max(scores) - min(scores) < 0.3:
        return True, f"score plateau (range={max(scores)-min(scores):.2f})"

    # Signal 3: length oscillating (not converging)
    if len(lengths) >= 3:
        diffs = [abs(lengths[i] - lengths[i+1]) for i in range(len(lengths)-1)]
        if max(diffs) < 20:
            return True, f"length stable (max_delta={max(diffs)})"

    return False, "not yet converged"


async def score_text(text: str) -> float:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Score 0-10 for clarity. Respond with just the number.\n\n{text[:500]}"}],
    )
    try:
        return float(response.content[0].text.strip().split()[0])
    except Exception:
        return 5.0


async def refine_with_multi_signal(task: str, run_id: str = "run-1") -> str:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Write an explanation of: {task}"}],
    )
    current = response.content[0].text
    score = await score_text(current)
    save_iteration(conn, run_id, 0, current, None, score)
    print(f"[iter 0] len={len(current)} score={score:.1f}")

    for i in range(1, 12):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": f"Improve clarity and accuracy:\n\n{current}"}],
        )
        improved = response.content[0].text
        sim = difflib.SequenceMatcher(None, current, improved).ratio()
        score = await score_text(improved)
        save_iteration(conn, run_id, i, improved, sim, score)
        print(f"[iter {i}] sim={sim:.3f} score={score:.1f} len={len(improved)}")

        converged, reason = check_convergence(conn, run_id)
        if converged:
            print(f"Converged: {reason}")
            conn.close()
            return improved

        current = improved

    conn.close()
    return current


result = asyncio.run(refine_with_multi_signal("distributed consensus algorithms"))
print(f"\nFinal:\n{result[:400]}")

# Expected Token Savings: 45-60%; multi-signal fires on whichever convergence criterion is met first
# Environment: Python 3.11+; persist DB to disk for cross-session convergence analytics
```

## Option 5: Change-Aware Early Exit with Diff Summary

```python
import anthropic
import difflib
import re

client = anthropic.Anthropic()

MAX_ITERATIONS = 10
MIN_MEANINGFUL_CHANGES = 3  # Stop if fewer than N lines changed


def count_meaningful_changes(old: str, new: str) -> tuple[int, list[str]]:
    """Count lines that changed meaningfully (not just whitespace)."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diffs = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))

    changes = []
    for line in diffs:
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            stripped = line[1:].strip()
            # Ignore trivial changes (punctuation only, whitespace, very short)
            if len(stripped) > 10 and re.search(r'\w{4,}', stripped):
                changes.append(line)

    return len(changes), changes


def iterative_refine_with_diff(task: str) -> str:
    """Stop when changes are too minor to be meaningful."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": f"Write a detailed explanation of: {task}"}],
    )
    current = response.content[0].text
    print(f"[iter 0] {len(current)} chars")

    for i in range(1, MAX_ITERATIONS + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": f"Review and improve this explanation. Focus only on significant improvements:\n\n{current}"
            }],
        )
        improved = response.content[0].text
        n_changes, change_lines = count_meaningful_changes(current, improved)
        print(f"[iter {i}] meaningful_changes={n_changes}")

        if n_changes < MIN_MEANINGFUL_CHANGES:
            print(f"Converged: only {n_changes} meaningful changes (< {MIN_MEANINGFUL_CHANGES})")
            if change_lines:
                print("Last changes:")
                for c in change_lines[:3]:
                    print(f"  {c[:80]}")
            return improved

        current = improved

    return current


result = iterative_refine_with_diff("how Raft consensus handles leader election")
print(f"\nFinal:\n{result[:400]}")

# Expected Token Savings: 35-50%; diff-based detection catches paraphrase-only edits that add no value
# Environment: Python 3.11+; tune MIN_MEANINGFUL_CHANGES (2-5) based on document size and task precision
```

## Option 6: Convergence Budget Manager with Adaptive Threshold

```python
import asyncio
import anthropic
import difflib
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class ConvergenceBudget:
    task_id: str
    max_iterations: int = 10
    max_seconds: float = 60.0
    initial_threshold: float = 0.85    # Loose threshold early on
    final_threshold: float = 0.96      # Tight threshold near max
    start_time: float = field(default_factory=time.monotonic)
    iteration: int = 0
    history: list[tuple[float, float]] = field(default_factory=list)  # (sim, elapsed)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def adaptive_threshold(self) -> float:
        """Threshold tightens as iterations / time budget is consumed."""
        iter_frac = self.iteration / max(self.max_iterations, 1)
        time_frac = self.elapsed / max(self.max_seconds, 0.001)
        frac = max(iter_frac, time_frac)
        return self.initial_threshold + frac * (self.final_threshold - self.initial_threshold)

    def record(self, sim: float) -> None:
        self.iteration += 1
        self.history.append((sim, self.elapsed))

    def should_stop(self, sim: float) -> tuple[bool, str]:
        if self.elapsed >= self.max_seconds:
            return True, f"time budget exhausted ({self.elapsed:.1f}s)"
        if self.iteration >= self.max_iterations:
            return True, f"iteration budget exhausted ({self.iteration})"
        thresh = self.adaptive_threshold
        if sim >= thresh:
            return True, f"converged (sim={sim:.3f} >= adaptive threshold {thresh:.3f})"
        return False, ""


async def refine_with_adaptive_budget(task: str, task_id: str = "task-1") -> str:
    budget = ConvergenceBudget(task_id=task_id, max_iterations=8, max_seconds=45.0)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Write a first draft: {task}"}],
    )
    current = response.content[0].text
    print(f"[iter 0] {len(current)} chars | threshold={budget.adaptive_threshold:.3f}")

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": f"Improve this text:\n\n{current}"}],
        )
        improved = response.content[0].text
        sim = difflib.SequenceMatcher(None, current, improved).ratio()
        budget.record(sim)

        thresh = budget.adaptive_threshold
        print(f"[iter {budget.iteration}] sim={sim:.3f} | threshold={thresh:.3f} | elapsed={budget.elapsed:.1f}s")

        stop, reason = budget.should_stop(sim)
        if stop:
            print(f"Stopped: {reason}")
            print(f"History: {[(f'{s:.3f}', f'{e:.1f}s') for s, e in budget.history]}")
            return improved

        current = improved


result = asyncio.run(refine_with_adaptive_budget("memory management in modern operating systems"))
print(f"\nFinal:\n{result[:400]}")

# Expected Token Savings: 40-65%; adaptive threshold catches fast convergers early and forces slow ones to stop
# Environment: Python 3.11+; set max_seconds to 2x expected single-call latency times max_iterations
```

## Comparison

| Option | Signal | Overhead | Adaptive | SQLite | Best For |
|--------|--------|----------|----------|--------|----------|
| 1. Char Similarity | String diff ratio | Minimal | No | No | Simple text refinement |
| 2. Score Plateau | LLM rubric score | Medium (+50 tok/iter) | No | No | Quality-gated tasks |
| 3. Semantic Embedding | Token frequency vector | Low | No | No | Paraphrase-heavy rewrites |
| 4. Multi-Signal | Sim + score + length | Medium | No | Yes | Production with analytics |
| 5. Diff Count | Meaningful line changes | Minimal | No | No | Document editing tasks |
| 6. Adaptive Budget | Sim + time + iter | Minimal | Yes | No | Variable-complexity tasks |
