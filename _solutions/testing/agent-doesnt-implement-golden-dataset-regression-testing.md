---
layout: solution
title: "Agent Doesn't Implement Golden Dataset Regression Testing"
category: testing
description: "Agents without a golden dataset can't detect when a prompt change, model upgrade, or refactor silently degrades output quality — golden dataset testing catches regressions before they reach production."
tags: [testing, golden-dataset, regression-testing, evals, quality, ci-cd, llm-testing]
---

# Agent Doesn't Implement Golden Dataset Regression Testing

## Problem

AI agents are sensitive to small changes: a rephrased system prompt, a new model version, a refactored tool call, or a changed few-shot example can all silently degrade output quality. Unit tests verify code paths but can't catch "the model now gives worse answers." Without a golden dataset of representative inputs with known-good outputs, there's no way to detect these regressions before they reach users.

Golden dataset regression testing captures a curated set of inputs paired with quality criteria — either exact expected outputs, rubric scores, or embedding similarity thresholds — and runs them automatically in CI to flag quality regressions.

## Solutions

### Option 1: Exact Match Golden Dataset with JSON Fixtures

Store input/expected-output pairs as JSON. Evaluate model outputs against exact or near-exact expected strings.

```python
import json
import anthropic
import pytest
from pathlib import Path
from difflib import SequenceMatcher

client = anthropic.Anthropic()

# Golden dataset — in production, load from golden_dataset.json
GOLDEN_DATASET = [
    {
        "id": "greet_001",
        "input": "What is 2 + 2?",
        "expected": "4",
        "match_type": "contains",  # "exact", "contains", "similarity"
        "min_similarity": 0.9,
        "tags": ["math", "basic"],
    },
    {
        "id": "format_001",
        "input": "List three Python data types.",
        "expected_contains": ["int", "str", "list"],
        "match_type": "contains_all",
        "tags": ["python", "knowledge"],
    },
    {
        "id": "tone_001",
        "input": "Explain async/await in one sentence.",
        "match_type": "length",
        "max_length": 200,  # Must be concise
        "tags": ["python", "async"],
    },
]

SYSTEM_PROMPT = "You are a concise Python programming assistant."

def get_agent_response(user_input: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text.strip()

def evaluate_case(case: dict, actual: str) -> dict:
    match_type = case["match_type"]
    passed = False
    reason = ""

    if match_type == "exact":
        passed = actual == case["expected"]
        reason = f"Expected: {case['expected']!r}, Got: {actual!r}"

    elif match_type == "contains":
        passed = case["expected"].lower() in actual.lower()
        reason = f"Expected to contain {case['expected']!r}"

    elif match_type == "contains_all":
        missing = [t for t in case["expected_contains"] if t.lower() not in actual.lower()]
        passed = len(missing) == 0
        reason = f"Missing: {missing}" if missing else "All terms found"

    elif match_type == "similarity":
        score = SequenceMatcher(None, case["expected"].lower(), actual.lower()).ratio()
        passed = score >= case.get("min_similarity", 0.8)
        reason = f"Similarity: {score:.2f} (min={case.get('min_similarity', 0.8)})"

    elif match_type == "length":
        passed = len(actual) <= case.get("max_length", 500)
        reason = f"Length: {len(actual)} chars (max={case.get('max_length', 500)})"

    return {"case_id": case["id"], "passed": passed, "reason": reason, "actual": actual[:100]}

@pytest.mark.parametrize("case", GOLDEN_DATASET, ids=[c["id"] for c in GOLDEN_DATASET])
def test_golden_case(case):
    actual = get_agent_response(case["input"])
    result = evaluate_case(case, actual)
    assert result["passed"], f"Golden case {case['id']} failed: {result['reason']}\nActual: {result['actual']}"

if __name__ == "__main__":
    results = []
    for case in GOLDEN_DATASET:
        actual = get_agent_response(case["input"])
        result = evaluate_case(case, actual)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {case['id']}: {result['reason']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n{passed}/{len(results)} cases passed")
# Expected Token Savings: Catches regressions early — prevents costly production rollbacks
# Environment: Any agent with a fixed system prompt; CI/CD pipelines
```

### Option 2: LLM-as-Judge Rubric Scoring

When outputs can't be exactly matched, use a separate model call to evaluate quality against a rubric. Store rubric scores over time to detect degradation.

```python
import anthropic
import json
import sqlite3
import time
from datetime import datetime

client = anthropic.Anthropic()

GOLDEN_CASES = [
    {
        "id": "explain_001",
        "input": "Explain Python decorators to a beginner.",
        "rubric": {
            "clarity": "Explanation is clear and avoids jargon",
            "accuracy": "Technically correct description of decorators",
            "example": "Includes at least one concrete example",
            "length": "Between 50 and 300 words",
        },
        "min_score": 3.0,  # Out of 4.0
    },
    {
        "id": "debug_001",
        "input": "Why would `for i in range(10): print(i)` print nothing?",
        "rubric": {
            "correctness": "Identifies that it DOES print 0-9, or asks for more context",
            "helpfulness": "Provides a useful response or clarification",
            "conciseness": "Doesn't over-explain a simple situation",
        },
        "min_score": 2.5,
    },
]

def run_agent(user_input: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system="You are a Python programming assistant.",
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text

def judge_response(case: dict, actual_response: str) -> dict:
    rubric_text = "\n".join(
        f"- {criterion}: {description}"
        for criterion, description in case["rubric"].items()
    )
    n_criteria = len(case["rubric"])

    judge_prompt = f"""Evaluate this AI response on a rubric. Score each criterion 0 (fail) or 1 (pass).

Question asked: {case['input']}

AI Response:
{actual_response}

Rubric ({n_criteria} criteria):
{rubric_text}

Return JSON only:
{{
  "scores": {{"criterion_name": 0_or_1, ...}},
  "total": <sum>,
  "max": {n_criteria},
  "verdict": "pass" or "fail",
  "notes": "brief explanation"
}}"""

    judge_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": judge_prompt}],
    )

    try:
        return json.loads(judge_resp.content[0].text)
    except json.JSONDecodeError:
        return {"total": 0, "max": n_criteria, "verdict": "error", "notes": "Judge parse failed"}

def store_result(conn: sqlite3.Connection, case_id: str, score: float, max_score: float, model: str):
    conn.execute(
        "INSERT INTO scores (case_id, score, max_score, model, run_at) VALUES (?, ?, ?, ?, ?)",
        (case_id, score, max_score, model, datetime.now().isoformat())
    )
    conn.commit()

def detect_regression(conn: sqlite3.Connection, case_id: str, threshold: float = 0.15) -> bool:
    """Return True if latest score dropped >15% from historical average."""
    rows = conn.execute(
        "SELECT score, max_score FROM scores WHERE case_id = ? ORDER BY run_at DESC LIMIT 10",
        (case_id,)
    ).fetchall()
    if len(rows) < 2:
        return False
    latest = rows[0][0] / rows[0][1]
    historical_avg = sum(r[0] / r[1] for r in rows[1:]) / (len(rows) - 1)
    return (historical_avg - latest) > threshold

conn = sqlite3.connect(":memory:")
conn.execute("""CREATE TABLE scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT, score REAL, max_score REAL, model TEXT, run_at TEXT
)""")

for case in GOLDEN_CASES:
    actual = run_agent(case["input"])
    judgment = judge_response(case, actual)
    score = judgment.get("total", 0)
    max_s = judgment.get("max", len(case["rubric"]))

    store_result(conn, case["id"], score, max_s, "claude-haiku-4-5-20251001")
    regression = detect_regression(conn, case["id"])

    status = "PASS" if score >= case["min_score"] else "FAIL"
    reg_flag = " [REGRESSION DETECTED]" if regression else ""
    print(f"[{status}] {case['id']}: {score}/{max_s} — {judgment.get('notes', '')}{reg_flag}")
# Expected Token Savings: Catches quality regressions before they cost user trust and re-runs
# Environment: Production agents; model upgrade validation; prompt A/B testing
```

### Option 3: Embedding Similarity Regression Baseline

Embed both the golden reference answer and the model's actual answer. Flag when cosine similarity drops below a threshold — catches semantic drift even when phrasing changes.

```python
import anthropic
import numpy as np
import json
import sqlite3
from datetime import datetime

client = anthropic.Anthropic()

def embed(text: str) -> list[float]:
    # Stub: replace with real embedding model (e.g., text-embedding-3-small, voyage-3)
    # For demo, use a simple hash-based fake embedding
    import hashlib
    words = text.lower().split()
    vec = np.zeros(64)
    for i, word in enumerate(words[:64]):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[i % 64] += (h % 1000) / 1000.0
    norm = np.linalg.norm(vec)
    return (vec / norm if norm > 0 else vec).tolist()

def cosine_sim(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom > 0 else 0.0

GOLDEN_DATASET = [
    {
        "id": "explain_async",
        "input": "What is asyncio in Python?",
        "golden_answer": (
            "asyncio is Python's standard library for writing concurrent code using "
            "async/await syntax. It uses an event loop to run coroutines, enabling "
            "non-blocking I/O operations without threads."
        ),
        "min_similarity": 0.75,
    },
    {
        "id": "explain_gil",
        "input": "What is the Python GIL?",
        "golden_answer": (
            "The GIL (Global Interpreter Lock) is a mutex in CPython that prevents "
            "multiple threads from executing Python bytecode simultaneously, "
            "limiting true parallelism for CPU-bound tasks."
        ),
        "min_similarity": 0.72,
    },
]

def run_and_evaluate(case: dict) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="You are a Python expert. Answer concisely.",
        messages=[{"role": "user", "content": case["input"]}],
    )
    actual = resp.content[0].text.strip()

    golden_emb = embed(case["golden_answer"])
    actual_emb = embed(actual)
    similarity = cosine_sim(golden_emb, actual_emb)

    passed = similarity >= case["min_similarity"]
    return {
        "id": case["id"],
        "passed": passed,
        "similarity": round(similarity, 3),
        "threshold": case["min_similarity"],
        "actual_preview": actual[:120],
    }

# Run baseline
conn = sqlite3.connect(":memory:")
conn.execute("""CREATE TABLE baseline (
    id TEXT, run_date TEXT, similarity REAL, passed INTEGER
)""")

for case in GOLDEN_DATASET:
    result = run_and_evaluate(case)
    conn.execute(
        "INSERT INTO baseline VALUES (?, ?, ?, ?)",
        (result["id"], datetime.now().isoformat(), result["similarity"], int(result["passed"]))
    )
    conn.commit()

    flag = "PASS" if result["passed"] else "FAIL"
    print(f"[{flag}] {result['id']}: similarity={result['similarity']} "
          f"(threshold={result['threshold']})")
    print(f"   Preview: {result['actual_preview']}")

# Check for regressions against historical baseline
print("\n--- Regression Check ---")
for case in GOLDEN_DATASET:
    history = conn.execute(
        "SELECT similarity FROM baseline WHERE id = ? ORDER BY run_date ASC",
        (case["id"],)
    ).fetchall()
    if len(history) > 1:
        first_sim = history[0][0]
        latest_sim = history[-1][0]
        delta = latest_sim - first_sim
        print(f"{case['id']}: baseline={first_sim:.3f} → latest={latest_sim:.3f} "
              f"(delta={delta:+.3f})")
# Expected Token Savings: Semantic drift caught early = no blind model rollout costs
# Environment: Model upgrade validation; multilingual agents; paraphrase-tolerant evals
```

### Option 4: Behavioral Invariant Testing

Instead of matching outputs, verify behavioral invariants — properties that must always hold regardless of phrasing. These are more robust to model updates than exact-match tests.

```python
import anthropic
import pytest
import re
from typing import Callable

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a Python assistant. Rules:
- Always include code examples when explaining concepts
- Never claim Python is slower than compiled languages without qualification
- Always mention type hints when discussing function signatures
- Response must be in English
"""

def get_response(user_input: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text

# Define invariants as named functions
def has_code_block(response: str) -> bool:
    """Response must contain a code block when explaining code concepts."""
    return "```" in response or "    " in response  # fenced or indented

def is_english(response: str) -> bool:
    """Response must be in English."""
    english_words = {"the", "is", "are", "and", "or", "a", "an", "in", "to", "of", "you"}
    words = set(response.lower().split())
    return len(words & english_words) >= 3

def reasonable_length(response: str) -> bool:
    """Response must be between 20 and 800 characters."""
    return 20 <= len(response) <= 800

def no_apology_spam(response: str) -> bool:
    """Must not start with excessive apologies."""
    apology_starters = ["i apologize", "i'm sorry", "i am sorry", "sorry,"]
    first_line = response.lower().strip().split("\n")[0]
    return not any(first_line.startswith(a) for a in apology_starters)

# Test cases with required invariants
INVARIANT_CASES = [
    {
        "id": "explain_list_comp",
        "input": "Explain list comprehensions.",
        "required_invariants": ["has_code_block", "is_english", "reasonable_length"],
    },
    {
        "id": "define_decorator",
        "input": "What is a decorator?",
        "required_invariants": ["has_code_block", "is_english", "reasonable_length", "no_apology_spam"],
    },
    {
        "id": "short_question",
        "input": "Is Python interpreted?",
        "required_invariants": ["is_english", "reasonable_length"],
    },
]

INVARIANT_FUNCTIONS: dict[str, Callable[[str], bool]] = {
    "has_code_block": has_code_block,
    "is_english": is_english,
    "reasonable_length": reasonable_length,
    "no_apology_spam": no_apology_spam,
}

@pytest.mark.parametrize("case", INVARIANT_CASES, ids=[c["id"] for c in INVARIANT_CASES])
def test_behavioral_invariants(case):
    response = get_response(case["input"])
    failures = []
    for invariant_name in case["required_invariants"]:
        fn = INVARIANT_FUNCTIONS[invariant_name]
        if not fn(response):
            failures.append(invariant_name)
    assert not failures, (
        f"Case {case['id']} violated invariants: {failures}\n"
        f"Response: {response[:200]}"
    )

if __name__ == "__main__":
    for case in INVARIANT_CASES:
        response = get_response(case["input"])
        failures = [
            name for name in case["required_invariants"]
            if not INVARIANT_FUNCTIONS[name](response)
        ]
        status = "PASS" if not failures else f"FAIL ({', '.join(failures)})"
        print(f"[{status}] {case['id']}")
# Expected Token Savings: Behavioral invariants catch 80% of regressions with fewer API calls
# Environment: Agents with explicit behavioral rules; safety-critical agents; style-governed chatbots
```

### Option 5: Snapshot Update Workflow

Capture model outputs as "snapshots." On each CI run, compare against stored snapshots. When a change is intentional, update snapshots explicitly — just like Jest snapshot testing.

```python
import anthropic
import json
import hashlib
import os
from pathlib import Path
from datetime import datetime

client = anthropic.Anthropic()

SNAPSHOT_DIR = Path("/tmp/agent_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)

SYSTEM_PROMPT = "You are a Python assistant. Be concise."

SNAPSHOT_INPUTS = [
    {"id": "what_is_list", "input": "What is a Python list?"},
    {"id": "what_is_dict", "input": "What is a Python dictionary?"},
    {"id": "what_is_set",  "input": "What is a Python set?"},
]

def get_response(user_input: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text.strip()

def snapshot_path(case_id: str) -> Path:
    return SNAPSHOT_DIR / f"{case_id}.json"

def load_snapshot(case_id: str) -> dict | None:
    p = snapshot_path(case_id)
    if p.exists():
        return json.loads(p.read_text())
    return None

def save_snapshot(case_id: str, response: str):
    snap = {
        "case_id": case_id,
        "response": response,
        "response_hash": hashlib.sha256(response.encode()).hexdigest()[:16],
        "captured_at": datetime.now().isoformat(),
        "model": "claude-haiku-4-5-20251001",
        "system_hash": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:16],
    }
    snapshot_path(case_id).write_text(json.dumps(snap, indent=2))
    print(f"Snapshot saved: {case_id}")

def compare_to_snapshot(case_id: str, actual: str, update: bool = False) -> dict:
    existing = load_snapshot(case_id)

    if existing is None:
        save_snapshot(case_id, actual)
        return {"status": "created", "case_id": case_id}

    if update:
        save_snapshot(case_id, actual)
        return {"status": "updated", "case_id": case_id}

    # Compare hashes for exact match
    actual_hash = hashlib.sha256(actual.encode()).hexdigest()[:16]
    if actual_hash == existing["response_hash"]:
        return {"status": "match", "case_id": case_id}

    # Compute similarity for soft comparison
    from difflib import SequenceMatcher
    similarity = SequenceMatcher(None, existing["response"], actual).ratio()
    return {
        "status": "changed",
        "case_id": case_id,
        "similarity": round(similarity, 3),
        "old_preview": existing["response"][:80],
        "new_preview": actual[:80],
        "old_hash": existing["response_hash"],
        "new_hash": actual_hash,
    }

def run_snapshot_tests(update: bool = False):
    results = {"match": 0, "changed": 0, "created": 0, "updated": 0}
    for case in SNAPSHOT_INPUTS:
        actual = get_response(case["input"])
        result = compare_to_snapshot(case["id"], actual, update=update)
        results[result["status"]] += 1

        if result["status"] == "changed":
            print(f"[CHANGED] {case['id']}: similarity={result.get('similarity')}")
            print(f"  Old: {result.get('old_preview')}")
            print(f"  New: {result.get('new_preview')}")
            print(f"  Run with --update to accept new snapshots")
        else:
            print(f"[{result['status'].upper()}] {case['id']}")

    print(f"\nSummary: {results}")
    return results["changed"] == 0  # Pass if no unexpected changes

import sys
update_mode = "--update" in sys.argv
passed = run_snapshot_tests(update=update_mode)
sys.exit(0 if passed else 1)
# Expected Token Savings: Snapshots catch silent changes; prevents undetected prompt regressions
# Environment: CI pipelines; pre-deployment checks; model version upgrades
```

### Option 6: Automated Golden Set Expansion from Production Logs

Sample real production queries periodically, have the model evaluate its own response quality, and promote high-quality examples to the golden dataset automatically.

```python
import anthropic
import json
import random
import sqlite3
from datetime import datetime

client = anthropic.Anthropic()

# Simulate production query log
PRODUCTION_LOG = [
    {"query": "How do I reverse a string in Python?", "response": "Use slicing: `s[::-1]`"},
    {"query": "What is a lambda in Python?", "response": "A lambda is an anonymous function: `lambda x: x*2`"},
    {"query": "How do I read a file in Python?", "response": "Use `open('file.txt', 'r').read()` or a context manager."},
    {"query": "What is __init__ in Python?", "response": "The constructor method called when creating an object instance."},
    {"query": "How do list and tuple differ?", "response": "Lists are mutable, tuples are immutable."},
]

def self_evaluate(query: str, response: str) -> dict:
    """Use the model to score its own past response."""
    eval_prompt = f"""Rate this AI response to a Python question. Return JSON only.

Question: {query}
Response: {response}

Score on:
- accuracy (0-3): Is the response technically correct?
- helpfulness (0-3): Does it actually help the user?
- conciseness (0-2): Is it appropriately brief?

JSON format:
{{"accuracy": N, "helpfulness": N, "conciseness": N, "total": N, "max": 8, "promote": true/false}}

Set "promote" to true if total >= 6."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": eval_prompt}],
    )
    try:
        return json.loads(resp.content[0].text)
    except json.JSONDecodeError:
        return {"total": 0, "max": 8, "promote": False}

def build_golden_from_production(
    logs: list[dict],
    sample_rate: float = 0.4,
    min_score: int = 6,
) -> list[dict]:
    """Sample production logs and promote high-quality examples to golden set."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE golden (
        id INTEGER PRIMARY KEY, query TEXT, response TEXT, score INTEGER,
        promoted_at TEXT
    )""")

    sample = random.sample(logs, max(1, int(len(logs) * sample_rate)))
    promoted = []

    for item in sample:
        evaluation = self_evaluate(item["query"], item["response"])
        score = evaluation.get("total", 0)
        should_promote = evaluation.get("promote", False) and score >= min_score

        if should_promote:
            conn.execute(
                "INSERT INTO golden (query, response, score, promoted_at) VALUES (?, ?, ?, ?)",
                (item["query"], item["response"], score, datetime.now().isoformat())
            )
            conn.commit()
            promoted.append({
                "query": item["query"],
                "expected_response": item["response"],
                "quality_score": score,
            })
            print(f"PROMOTED (score={score}/8): {item['query'][:50]}...")
        else:
            print(f"SKIPPED  (score={score}/8): {item['query'][:50]}...")

    print(f"\nPromoted {len(promoted)}/{len(sample)} sampled examples to golden set")
    return promoted

golden = build_golden_from_production(PRODUCTION_LOG, sample_rate=0.6, min_score=5)
print(f"\nGolden dataset size: {len(golden)}")
for g in golden:
    print(f"  [{g['quality_score']}/8] {g['query']}")
# Expected Token Savings: Continuous quality monitoring catches drift before user complaints
# Environment: Production agents with high query volume; periodic quality audits
```

## Comparison Table

| Option | Matching Method | Human Effort | Robustness to Rephrasing | CI Integration | Best For |
|--------|----------------|--------------|--------------------------|----------------|----------|
| 1: Exact/Contains Match | String matching | Low | Low | Easy | Simple factual outputs, format checks |
| 2: LLM-as-Judge Rubric | Model evaluation | Medium | High | Medium | Open-ended quality, tone, helpfulness |
| 3: Embedding Similarity | Cosine similarity | Low | High | Medium | Semantic drift detection |
| 4: Behavioral Invariants | Property functions | Medium | Very High | Easy | Safety rules, style enforcement |
| 5: Snapshot Testing | Hash + diff | Very Low | Low | Very Easy | Detecting any output change |
| 6: Production Promotion | Auto-sampling + eval | Very Low | High | Medium | Growing golden set from real traffic |
