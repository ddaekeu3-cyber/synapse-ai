---
layout: solution
title: "Agent Doesn't Implement Automatic Prompt Optimization"
category: prompt-engineering
description: "Automatically improve prompts using evaluation-guided search, few-shot bootstrapping, and gradient-free optimization — so prompt quality improves without manual rewriting."
tags: [prompt-engineering, optimization, evals, dspy, few-shot, python]
---

# Agent Doesn't Implement Automatic Prompt Optimization

Handwritten prompts plateau quickly. Automatic prompt optimization uses evaluation feedback to iteratively refine instructions, discover better few-shot examples, and select phrasing that consistently improves task performance — without requiring human intuition for each edit.

## Option 1: Eval-Scored Candidate Search

```python
import anthropic
import random

client = anthropic.Anthropic()

CANDIDATE_INSTRUCTIONS = [
    "Answer concisely in one sentence.",
    "Provide a precise, technical answer.",
    "Explain step-by-step, then give a final answer.",
    "Answer as an expert. Be direct and accurate.",
    "Think carefully, then answer clearly.",
]

def evaluate_prompt(instruction: str, examples: list[dict]) -> float:
    """Score a prompt instruction against labeled examples."""
    correct = 0
    for ex in examples:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=instruction,
            messages=[{"role": "user", "content": ex["input"]}],
        )
        output = resp.content[0].text.strip().lower()
        if ex["expected"].lower() in output:
            correct += 1
    return correct / len(examples)

def find_best_instruction(examples: list[dict]) -> tuple[str, float]:
    best_instr, best_score = "", 0.0
    for instr in CANDIDATE_INSTRUCTIONS:
        score = evaluate_prompt(instr, examples)
        print(f"Score={score:.2f}: {instr[:60]}")
        if score > best_score:
            best_score, best_instr = score, instr
    return best_instr, best_score

# Labeled eval set
eval_set = [
    {"input": "What is the capital of France?",  "expected": "paris"},
    {"input": "What does CPU stand for?",        "expected": "central processing unit"},
    {"input": "What is 12 * 12?",               "expected": "144"},
]

best, score = find_best_instruction(eval_set)
print(f"\nBest instruction (score={score:.2f}):\n{best}")

# Expected Token Savings: Haiku for eval search; only deploy winning prompt to Opus/Sonnet
# Environment: any; works with any labeled eval set; scales with more candidates
```

## Option 2: Few-Shot Example Bootstrap and Selection

```python
import anthropic
import itertools

client = anthropic.Anthropic()

BASE_INSTRUCTION = "You are a precise question-answering assistant."

CANDIDATE_EXAMPLES = [
    ("What is 2+2?",                       "4"),
    ("What is the capital of Germany?",    "Berlin"),
    ("What color is the sky?",             "Blue"),
    ("What does HTTP stand for?",          "HyperText Transfer Protocol"),
    ("How many days in a week?",           "7"),
]

def format_few_shot(examples: list[tuple]) -> str:
    shots = "\n".join(f"Q: {q}\nA: {a}" for q, a in examples)
    return f"{BASE_INSTRUCTION}\n\nExamples:\n{shots}"

def score_few_shot_set(shots: list[tuple], eval_set: list[dict]) -> float:
    system_prompt = format_few_shot(shots)
    correct = 0
    for ex in eval_set:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=system_prompt,
            messages=[{"role": "user", "content": ex["input"]}],
        )
        if ex["expected"].lower() in resp.content[0].text.lower():
            correct += 1
    return correct / len(eval_set)

eval_set = [
    {"input": "What is the boiling point of water in Celsius?", "expected": "100"},
    {"input": "What is the speed of light?",                    "expected": "299"},
    {"input": "How many continents are there?",                 "expected": "7"},
]

best_combo, best_score = [], 0.0
# Try all combinations of 2 examples from the pool
for combo in itertools.combinations(CANDIDATE_EXAMPLES, 2):
    score = score_few_shot_set(list(combo), eval_set)
    print(f"Score={score:.2f} | {[q for q,_ in combo]}")
    if score > best_score:
        best_score, best_combo = score, list(combo)

print(f"\nBest few-shot set (score={best_score:.2f}):")
for q, a in best_combo:
    print(f"  Q: {q} -> A: {a}")

# Expected Token Savings: 30-50% vs manual prompt engineering iteration cycles
# Environment: small eval sets; combinatorial search works up to ~10 candidates
```

## Option 3: Iterative Instruction Refinement via LLM Critique

```python
import anthropic

client = anthropic.Anthropic()

def run_prompt(instruction: str, user_input: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=instruction,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text

def critique_and_improve(instruction: str, failures: list[dict]) -> str:
    """Ask the model to improve the instruction based on failures."""
    failure_text = "\n".join(
        f"Input: {f['input']}\nExpected: {f['expected']}\nGot: {f['got']}"
        for f in failures
    )
    critique_prompt = f"""You are a prompt engineer. The following instruction failed on these examples:

INSTRUCTION:
{instruction}

FAILURES:
{failure_text}

Rewrite the instruction to fix these failures. Return ONLY the new instruction text, nothing else."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": critique_prompt}],
    )
    return resp.content[0].text.strip()

def optimize_instruction(initial: str, eval_set: list[dict], rounds: int = 3) -> str:
    instruction = initial
    for round_num in range(1, rounds + 1):
        failures = []
        for ex in eval_set:
            output = run_prompt(instruction, ex["input"])
            if ex["expected"].lower() not in output.lower():
                failures.append({**ex, "got": output[:100]})
        print(f"Round {round_num}: {len(failures)}/{len(eval_set)} failures")
        if not failures:
            break
        instruction = critique_and_improve(instruction, failures)
        print(f"New instruction: {instruction[:100]}")
    return instruction

eval_set = [
    {"input": "Translate to Spanish: Good morning",     "expected": "buenos"},
    {"input": "Translate to Spanish: Thank you",        "expected": "gracias"},
    {"input": "Translate to Spanish: Where is the library?", "expected": "biblioteca"},
]

final = optimize_instruction(
    "You are a helpful assistant.",
    eval_set,
    rounds=3,
)
print(f"\nFinal optimized instruction:\n{final}")

# Expected Token Savings: Self-correcting loop converges in 2-3 rounds vs. dozens of manual edits
# Environment: Haiku for eval runs, Sonnet for critique; works with any task type
```

## Option 4: Temperature and Parameter Search

```python
import anthropic
import statistics

client = anthropic.Anthropic()

INSTRUCTION = "Answer the question accurately and concisely."

def evaluate_at_temperature(
    temp: float,
    top_p: float,
    eval_set: list[dict],
    runs_per_example: int = 3,
) -> dict:
    """Evaluate consistency and accuracy at given sampling parameters."""
    accuracy_scores = []
    consistency_scores = []

    for ex in eval_set:
        outputs = []
        for _ in range(runs_per_example):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                system=INSTRUCTION,
                messages=[{"role": "user", "content": ex["input"]}],
                temperature=temp,
                top_p=top_p,
            )
            outputs.append(resp.content[0].text.strip().lower())
        # Accuracy: at least one output contains expected
        accuracy_scores.append(
            1.0 if any(ex["expected"].lower() in o for o in outputs) else 0.0
        )
        # Consistency: fraction of outputs that agree with majority
        majority = max(set(outputs), key=outputs.count)
        consistency_scores.append(
            sum(1 for o in outputs if o == majority) / runs_per_example
        )

    return {
        "temperature": temp,
        "top_p": top_p,
        "accuracy": statistics.mean(accuracy_scores),
        "consistency": statistics.mean(consistency_scores),
        "score": statistics.mean(accuracy_scores) * 0.7 + statistics.mean(consistency_scores) * 0.3,
    }

eval_set = [
    {"input": "What is 15% of 200?",         "expected": "30"},
    {"input": "What is the square root of 81?", "expected": "9"},
    {"input": "How many hours in a day?",     "expected": "24"},
]

configs = [(0.0, 1.0), (0.3, 0.9), (0.7, 0.95), (1.0, 1.0)]
results = [evaluate_at_temperature(t, p, eval_set) for t, p in configs]
results.sort(key=lambda r: r["score"], reverse=True)

print("Parameter search results:")
for r in results:
    print(f"  temp={r['temperature']} top_p={r['top_p']} -> "
          f"accuracy={r['accuracy']:.2f} consistency={r['consistency']:.2f} score={r['score']:.2f}")
print(f"\nBest config: temp={results[0]['temperature']} top_p={results[0]['top_p']}")

# Expected Token Savings: Avoid over-sampling in production by finding optimal temperature once
# Environment: any; runs per example can be reduced for faster search with larger eval sets
```

## Option 5: Prompt Paraphrase Ensemble with Voting

```python
import anthropic
from collections import Counter

client = anthropic.Anthropic()

def generate_paraphrases(original_instruction: str, n: int = 4) -> list[str]:
    """Generate N paraphrases of a system instruction."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Generate {n} distinct paraphrases of this AI assistant instruction. "
                f"Return one per line, no numbering:\n\n{original_instruction}"
            ),
        }],
    )
    lines = [l.strip() for l in resp.content[0].text.strip().split("\n") if l.strip()]
    return lines[:n]

def ensemble_answer(
    instructions: list[str],
    user_input: str,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    """Call all instructions in parallel and return majority-vote answer."""
    outputs = []
    for instr in instructions:
        resp = client.messages.create(
            model=model,
            max_tokens=64,
            system=instr,
            messages=[{"role": "user", "content": user_input}],
        )
        outputs.append(resp.content[0].text.strip())

    # Simple majority vote by first-word token
    votes = Counter(o.split()[0].lower() for o in outputs if o)
    winner_token = votes.most_common(1)[0][0]
    # Return the full output whose first word matches
    for o in outputs:
        if o.lower().startswith(winner_token):
            return o
    return outputs[0]

base = "You are a factual question-answering assistant. Be concise and accurate."
variants = generate_paraphrases(base, n=4)
print("Prompt variants:")
for v in variants:
    print(f"  - {v[:80]}")

questions = [
    "What is the chemical symbol for gold?",
    "Who wrote Hamlet?",
    "What is the largest planet in our solar system?",
]
all_instructions = [base] + variants
for q in questions:
    answer = ensemble_answer(all_instructions, q)
    print(f"\nQ: {q}\nA: {answer}")

# Expected Token Savings: Ensemble reduces hallucination risk; smaller model with voting beats single large model
# Environment: any; voting overhead is small relative to reliability gain
```

## Option 6: SQLite-Tracked Prompt Version Registry with A/B Scoring

```python
import anthropic
import sqlite3
import time
import uuid
import statistics

client = anthropic.Anthropic()
DB = "prompt_registry.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS prompt_versions (
            version_id TEXT PRIMARY KEY,
            instruction TEXT,
            created_at REAL,
            is_active INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            version_id TEXT,
            eval_input TEXT,
            expected TEXT,
            got TEXT,
            correct INTEGER,
            ts REAL
        )
    """)
    con.commit(); con.close()

def register_prompt(instruction: str) -> str:
    vid = uuid.uuid4().hex[:8]
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO prompt_versions VALUES (?,?,?,0)",
                (vid, instruction, time.time()))
    con.commit(); con.close()
    print(f"Registered prompt version {vid}")
    return vid

def score_version(version_id: str, eval_set: list[dict]) -> float:
    con = sqlite3.connect(DB)
    instr = con.execute(
        "SELECT instruction FROM prompt_versions WHERE version_id=?", (version_id,)
    ).fetchone()[0]
    con.close()

    scores = []
    for ex in eval_set:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=instr,
            messages=[{"role": "user", "content": ex["input"]}],
        )
        got = resp.content[0].text.strip()
        correct = int(ex["expected"].lower() in got.lower())
        scores.append(correct)
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO eval_results VALUES (?,?,?,?,?,?)",
                    (version_id, ex["input"], ex["expected"], got[:200], correct, time.time()))
        con.commit(); con.close()
    return statistics.mean(scores)

def promote_best(eval_set: list[dict]):
    con = sqlite3.connect(DB)
    versions = con.execute("SELECT version_id FROM prompt_versions").fetchall()
    con.close()

    results = {}
    for (vid,) in versions:
        results[vid] = score_version(vid, eval_set)
        print(f"  version={vid} score={results[vid]:.2f}")

    best_vid = max(results, key=results.get)
    con = sqlite3.connect(DB)
    con.execute("UPDATE prompt_versions SET is_active=0")
    con.execute("UPDATE prompt_versions SET is_active=1 WHERE version_id=?", (best_vid,))
    con.commit(); con.close()
    print(f"\nPromoted {best_vid} as active (score={results[best_vid]:.2f})")

init_db()
v1 = register_prompt("Answer questions briefly.")
v2 = register_prompt("You are an expert assistant. Answer precisely and factually.")
v3 = register_prompt("Give the most accurate, concise answer possible.")

eval_set = [
    {"input": "What is the largest ocean?",    "expected": "pacific"},
    {"input": "What element is H2O made of?",  "expected": "hydrogen"},
    {"input": "How many sides does a hexagon have?", "expected": "6"},
]

print("Scoring all versions:")
promote_best(eval_set)

# Expected Token Savings: Persistent registry lets you compare prompts over time; promotes best without re-running
# Environment: SQLite persists across runs; extend with more eval sets per task type
```

## Comparison

| Option | Optimization Strategy | Search Space | Cost |
|--------|----------------------|-------------|------|
| 1 — Candidate Search | Predefined instruction variants | Fixed list | Low |
| 2 — Few-Shot Bootstrap | Combinatorial example selection | Exponential in pool size | Medium |
| 3 — LLM Critique Loop | Iterative failure-driven rewrite | Unbounded | Medium |
| 4 — Parameter Search | Temperature + top_p grid | Small discrete grid | Medium |
| 5 — Paraphrase Ensemble | Voting across instruction variants | Auto-generated | High |
| 6 — Version Registry | SQLite A/B with promotion | Multi-version history | Low overhead |
