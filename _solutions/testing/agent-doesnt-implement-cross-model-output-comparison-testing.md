---
layout: solution
title: "Agent Doesn't Implement Cross-Model Output Comparison Testing"
category: testing
description: "Compare outputs across model versions, sizes, and providers to detect behavioral regressions, quality changes, and unexpected capability shifts before deployment."
tags: [testing, cross-model, regression, comparison, quality-assurance]
---

# Agent Doesn't Implement Cross-Model Output Comparison Testing

## Problem

Upgrading model versions or switching between model tiers without systematic comparison leads to silent behavioral regressions — the new model passes all unit tests but produces subtly worse outputs, contradicts prior system behavior, or exhibits unexpected capability shifts.

## Solution Options

### Option 1: Side-by-Side Response Comparison with Structured Diff

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ModelResponse:
    model: str
    content: str
    input_tokens: int
    output_tokens: int

TEST_PROMPTS = [
    "Summarize the CAP theorem in one sentence.",
    "Write a Python function to detect if a string is a palindrome.",
    "What are the tradeoffs between REST and GraphQL?"
]

MODELS_TO_COMPARE = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
]

def get_model_response(prompt: str, model: str) -> ModelResponse:
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return ModelResponse(
        model=model,
        content=resp.content[0].text,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens
    )

def compare_responses(responses: list[ModelResponse]) -> dict:
    lengths = {r.model: len(r.content) for r in responses}
    baseline = responses[0]
    diffs = {}
    for r in responses[1:]:
        shared_words = set(baseline.content.lower().split()) & set(r.content.lower().split())
        all_words = set(baseline.content.lower().split()) | set(r.content.lower().split())
        overlap = len(shared_words) / max(len(all_words), 1)
        diffs[r.model] = {
            "length_delta_pct": round((lengths[r.model] - lengths[baseline.model]) / max(lengths[baseline.model], 1) * 100, 1),
            "word_overlap_vs_baseline": round(overlap, 3)
        }
    return {"lengths": lengths, "diffs_vs_baseline": diffs}

for prompt in TEST_PROMPTS:
    print(f"\nPrompt: {prompt[:60]}")
    responses = [get_model_response(prompt, m) for m in MODELS_TO_COMPARE]
    comparison = compare_responses(responses)
    for r in responses:
        print(f"  [{r.model}] {r.output_tokens} tokens: {r.content[:80]}...")
    print(f"  Comparison: {comparison['diffs_vs_baseline']}")

# Expected Token Savings: N/A (comparison test); reveals token-quality tradeoffs per model tier
# Environment: pre-deployment regression testing, model upgrade validation, A/B evaluation
```

### Option 2: LLM-as-Judge Cross-Model Evaluation

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class EvalResult:
    model: str
    response: str
    score: int
    reasoning: str

EVAL_CRITERIA = """Rate this response on a scale of 1-5 for:
1. Accuracy (factually correct)
2. Completeness (covers the key aspects)
3. Clarity (easy to understand)
Respond with JSON: {"accuracy": N, "completeness": N, "clarity": N, "reasoning": "..."}"""

def evaluate_response(prompt: str, response: str, judge_model: str = "claude-sonnet-4-6") -> dict:
    judge_prompt = f"Question: {prompt}\n\nResponse to evaluate:\n{response}\n\n{EVAL_CRITERIA}"
    result = client.messages.create(
        model=judge_model,
        max_tokens=256,
        messages=[{"role": "user", "content": judge_prompt}]
    )
    import json, re
    text = result.content[0].text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"accuracy": 0, "completeness": 0, "clarity": 0, "reasoning": "parse error"}

def run_cross_model_eval(test_cases: list[dict]) -> list[dict]:
    models = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
    results = []

    for tc in test_cases:
        prompt = tc["prompt"]
        case_results = {"prompt": prompt, "model_scores": {}}

        for model in models:
            resp = client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            response_text = resp.content[0].text
            scores = evaluate_response(prompt, response_text)
            avg_score = round(sum([scores.get("accuracy", 0), scores.get("completeness", 0), scores.get("clarity", 0)]) / 3, 2)
            case_results["model_scores"][model] = {
                "avg": avg_score,
                "detail": scores,
                "tokens": resp.usage.output_tokens
            }

        # Determine winner
        best = max(case_results["model_scores"].items(), key=lambda x: x[1]["avg"])
        case_results["winner"] = best[0]
        case_results["winner_score"] = best[1]["avg"]
        results.append(case_results)
        print(f"Prompt: {prompt[:50]}... | Winner: {best[0]} ({best[1]['avg']}/5)")

    return results

test_cases = [
    {"prompt": "Explain gradient descent in one paragraph."},
    {"prompt": "What is the difference between a mutex and a semaphore?"},
    {"prompt": "Write a regex to validate an email address."}
]
eval_results = run_cross_model_eval(test_cases)

# Expected Token Savings: ~40% using haiku as judge for clear-cut cases; sonnet only for borderline
# Environment: model selection decisions, cost-quality optimization, upgrade validation
```

### Option 3: Behavioral Regression Test Suite with Snapshot Comparison

```python
import anthropic
import json
import hashlib
from pathlib import Path
from datetime import datetime

client = anthropic.Anthropic()
SNAPSHOT_DIR = Path("/tmp/model_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)

REGRESSION_SUITE = [
    {
        "id": "tc_001",
        "prompt": "What year did World War II end?",
        "expected_contains": ["1945"],
        "must_not_contain": ["1944", "1946"]
    },
    {
        "id": "tc_002",
        "prompt": "Is Python or Java faster for CPU-bound tasks?",
        "expected_contains": ["Java", "faster"],
        "must_not_contain": []
    },
    {
        "id": "tc_003",
        "prompt": "Write a one-line Python lambda to square a number.",
        "expected_contains": ["lambda", "**2"],
        "must_not_contain": []
    }
]

def run_test_case(tc: dict, model: str) -> dict:
    resp = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": tc["prompt"]}]
    )
    text = resp.content[0].text
    passes = [kw for kw in tc["expected_contains"] if kw.lower() in text.lower()]
    fails = [kw for kw in tc["must_not_contain"] if kw.lower() in text.lower()]
    return {
        "id": tc["id"],
        "model": model,
        "passed_checks": len(passes) == len(tc["expected_contains"]) and len(fails) == 0,
        "expected_hits": passes,
        "unexpected_hits": fails,
        "response_hash": hashlib.md5(text.encode()).hexdigest()[:8],
        "text": text[:200]
    }

def save_snapshot(model: str, results: list[dict]) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SNAPSHOT_DIR / f"{model.replace('/', '_')}_{ts}.json"
    path.write_text(json.dumps(results, indent=2))
    return path

def load_latest_snapshot(model: str) -> list[dict] | None:
    pattern = f"{model.replace('/', '_')}_*.json"
    files = sorted(SNAPSHOT_DIR.glob(pattern))
    if not files:
        return None
    return json.loads(files[-1].read_text())

def compare_snapshots(old: list[dict], new: list[dict]) -> list[str]:
    regressions = []
    old_by_id = {r["id"]: r for r in old}
    for result in new:
        old_result = old_by_id.get(result["id"])
        if not old_result:
            continue
        if old_result["passed_checks"] and not result["passed_checks"]:
            regressions.append(f"REGRESSION {result['id']}: was passing, now failing")
        if old_result["response_hash"] != result["response_hash"]:
            regressions.append(f"CHANGED {result['id']}: response hash changed {old_result['response_hash']} -> {result['response_hash']}")
    return regressions

for model in ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]:
    print(f"\n=== {model} ===")
    results = [run_test_case(tc, model) for tc in REGRESSION_SUITE]
    snapshot_path = save_snapshot(model, results)

    old_snapshot = load_latest_snapshot(model)
    if old_snapshot:
        regressions = compare_snapshots(old_snapshot, results)
        if regressions:
            print(f"  REGRESSIONS DETECTED:")
            for r in regressions:
                print(f"    - {r}")
        else:
            print(f"  All {len(results)} tests stable vs prior snapshot")
    else:
        print(f"  No prior snapshot — baseline saved to {snapshot_path}")

    pass_count = sum(1 for r in results if r["passed_checks"])
    print(f"  Pass rate: {pass_count}/{len(results)}")

# Expected Token Savings: test runs are bounded by suite size; hashing avoids re-evaluation
# Environment: CI/CD pipelines, model upgrade gating, production quality monitoring
```

### Option 4: Parallel Model Racing with Consensus Detection

```python
import anthropic
import asyncio
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class RaceResult:
    model: str
    response: str
    tokens: int

async def query_model(prompt: str, model: str) -> RaceResult:
    resp = await async_client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return RaceResult(model=model, response=resp.content[0].text, tokens=resp.usage.output_tokens)

def compute_pairwise_agreement(responses: list[RaceResult]) -> dict:
    """Check word-level overlap between all model pairs."""
    agreement = {}
    for i, a in enumerate(responses):
        for b in responses[i+1:]:
            words_a = set(a.response.lower().split())
            words_b = set(b.response.lower().split())
            overlap = len(words_a & words_b) / max(len(words_a | words_b), 1)
            key = f"{a.model[:20]} vs {b.model[:20]}"
            agreement[key] = round(overlap, 3)
    return agreement

def detect_outlier(responses: list[RaceResult], agreement: dict) -> str | None:
    """Flag model whose responses consistently disagree with others."""
    model_agreement_scores: dict[str, list[float]] = {r.model: [] for r in responses}
    for pair_key, score in agreement.items():
        parts = pair_key.split(" vs ")
        if len(parts) == 2:
            for model in responses:
                for part in parts:
                    if model.model.startswith(part.strip()):
                        model_agreement_scores[model.model].append(score)

    avg_scores = {m: sum(scores)/max(len(scores), 1) for m, scores in model_agreement_scores.items()}
    if not avg_scores:
        return None
    min_model = min(avg_scores, key=lambda m: avg_scores[m])
    if avg_scores[min_model] < 0.3:
        return min_model
    return None

async def run_model_race(prompts: list[str]) -> None:
    models = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]

    for prompt in prompts:
        print(f"\nPrompt: {prompt[:60]}")
        tasks = [query_model(prompt, m) for m in models]
        results = await asyncio.gather(*tasks)

        agreement = compute_pairwise_agreement(list(results))
        outlier = detect_outlier(list(results), agreement)

        for r in results:
            flag = " [OUTLIER]" if r.model == outlier else ""
            print(f"  [{r.model}{flag}] {r.tokens}t: {r.response[:80]}...")

        print(f"  Agreement: {agreement}")
        if outlier:
            print(f"  WARNING: {outlier} disagrees significantly with other models")

prompts = [
    "What is the time complexity of quicksort in the average case?",
    "Name three functional programming languages.",
    "Is blockchain necessary for most enterprise applications?"
]
asyncio.run(run_model_race(prompts))

# Expected Token Savings: parallel execution cuts wall time by ~50%; async avoids serial blocking
# Environment: model selection, ensemble validation, detecting capability regressions
```

### Option 5: Semantic Drift Detection Across Model Versions

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

# Domain-specific evaluation: technical accuracy ground truth
GROUND_TRUTH_SUITE = [
    {
        "id": "algo_001",
        "prompt": "What is the space complexity of merge sort?",
        "ground_truth_keywords": ["O(n)", "linear", "auxiliary"],
        "anti_keywords": ["O(1)", "constant", "in-place"]
    },
    {
        "id": "net_001",
        "prompt": "What TCP flag is set to terminate a connection?",
        "ground_truth_keywords": ["FIN", "RST"],
        "anti_keywords": ["SYN", "ACK only"]
    },
    {
        "id": "sec_001",
        "prompt": "What hashing algorithm should NOT be used for passwords?",
        "ground_truth_keywords": ["MD5", "SHA-1", "SHA1"],
        "anti_keywords": ["bcrypt", "argon2", "scrypt"]
    }
]

def score_technical_accuracy(response: str, tc: dict) -> dict:
    text = response.lower()
    hits = [kw for kw in tc["ground_truth_keywords"] if kw.lower() in text]
    misses = [kw for kw in tc["anti_keywords"] if kw.lower() in text]
    score = len(hits) / max(len(tc["ground_truth_keywords"]), 1) - 0.5 * len(misses)
    return {
        "score": round(max(0, min(1, score)), 2),
        "correct_hits": hits,
        "incorrect_hits": misses
    }

def semantic_drift_report(baseline_model: str, candidate_model: str, suite: list[dict]) -> dict:
    report = {"baseline": baseline_model, "candidate": candidate_model, "cases": [], "summary": {}}
    total_drift = 0

    for tc in suite:
        responses = {}
        for model in [baseline_model, candidate_model]:
            resp = client.messages.create(
                model=model,
                max_tokens=256,
                messages=[{"role": "user", "content": tc["prompt"]}]
            )
            responses[model] = resp.content[0].text

        baseline_score = score_technical_accuracy(responses[baseline_model], tc)
        candidate_score = score_technical_accuracy(responses[candidate_model], tc)
        drift = candidate_score["score"] - baseline_score["score"]
        total_drift += abs(drift)

        case = {
            "id": tc["id"],
            "baseline_score": baseline_score["score"],
            "candidate_score": candidate_score["score"],
            "drift": round(drift, 2),
            "status": "IMPROVED" if drift > 0.1 else ("REGRESSED" if drift < -0.1 else "STABLE")
        }
        report["cases"].append(case)
        print(f"  {tc['id']}: baseline={baseline_score['score']:.2f} candidate={candidate_score['score']:.2f} [{case['status']}]")

    report["summary"] = {
        "avg_abs_drift": round(total_drift / max(len(suite), 1), 3),
        "regressions": sum(1 for c in report["cases"] if c["status"] == "REGRESSED"),
        "improvements": sum(1 for c in report["cases"] if c["status"] == "IMPROVED"),
        "stable": sum(1 for c in report["cases"] if c["status"] == "STABLE")
    }
    return report

print("=== Semantic Drift Report ===")
report = semantic_drift_report("claude-haiku-4-5-20251001", "claude-sonnet-4-6", GROUND_TRUTH_SUITE)
print(f"\nSummary: {json.dumps(report['summary'], indent=2)}")

# Expected Token Savings: ground-truth scoring avoids LLM judge overhead (~30% cheaper)
# Environment: domain-specific model evaluation, upgrade gating, accuracy regression detection
```

### Option 6: Cross-Model Consistency Test with Statistical Summary

```python
import anthropic
import statistics
import json
from collections import defaultdict

client = anthropic.Anthropic()

CONSISTENCY_PROMPTS = [
    {"id": "c1", "prompt": "List exactly 3 benefits of containerization.", "expected_count": 3},
    {"id": "c2", "prompt": "Name the 4 pillars of OOP.", "expected_count": 4},
    {"id": "c3", "prompt": "List 5 HTTP status code categories.", "expected_count": 5}
]

MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
RUNS_PER_MODEL = 3  # multiple samples to measure variance

def count_list_items(text: str) -> int:
    """Count numbered or bulleted list items."""
    import re
    numbered = len(re.findall(r'^\s*\d+[\.\)]\s', text, re.MULTILINE))
    bulleted = len(re.findall(r'^\s*[-*•]\s', text, re.MULTILINE))
    return max(numbered, bulleted)

def run_consistency_battery(prompts: list[dict], models: list[str], runs: int) -> dict:
    results = defaultdict(lambda: defaultdict(list))

    for tc in prompts:
        for model in models:
            for run in range(runs):
                resp = client.messages.create(
                    model=model,
                    max_tokens=256,
                    messages=[{"role": "user", "content": tc["prompt"]}]
                )
                text = resp.content[0].text
                item_count = count_list_items(text)
                correct = (item_count == tc["expected_count"])
                results[tc["id"]][model].append({
                    "run": run,
                    "item_count": item_count,
                    "correct": correct,
                    "tokens": resp.usage.output_tokens
                })

    # Compute statistics per model per test
    summary = {}
    for tc in prompts:
        summary[tc["id"]] = {"expected": tc["expected_count"], "models": {}}
        for model in models:
            runs_data = results[tc["id"]][model]
            counts = [r["item_count"] for r in runs_data]
            accuracy = sum(1 for r in runs_data if r["correct"]) / len(runs_data)
            summary[tc["id"]]["models"][model] = {
                "accuracy": round(accuracy, 2),
                "count_mean": round(statistics.mean(counts), 2),
                "count_stdev": round(statistics.stdev(counts) if len(counts) > 1 else 0, 2),
                "avg_tokens": round(statistics.mean(r["tokens"] for r in runs_data), 1)
            }

    return summary

print("Running cross-model consistency battery...")
summary = run_consistency_battery(CONSISTENCY_PROMPTS, MODELS, RUNS_PER_MODEL)

print("\n=== Results ===")
for test_id, data in summary.items():
    print(f"\n{test_id} (expected {data['expected']} items):")
    for model, stats in data["models"].items():
        print(f"  {model}: accuracy={stats['accuracy']:.0%} mean={stats['count_mean']} stdev={stats['count_stdev']} tokens={stats['avg_tokens']}")

# Identify best model per test
print("\n=== Recommendations ===")
for test_id, data in summary.items():
    best = max(data["models"].items(), key=lambda x: (x[1]["accuracy"], -x[1]["count_stdev"]))
    print(f"  {test_id}: use {best[0]} (accuracy={best[1]['accuracy']:.0%}, stdev={best[1]['count_stdev']})")

# Expected Token Savings: haiku delivers 80%+ accuracy on structured tasks at ~4x lower cost
# Environment: format-sensitive tasks, instruction-following evaluation, model tier selection
```

## Comparison

| Option | Approach | Best For | Coverage |
|--------|----------|----------|----------|
| 1 | Structural diff + word overlap | Quick pre-upgrade check | Surface |
| 2 | LLM-as-judge scoring | Quality regression detection | Deep |
| 3 | Snapshot hash + keyword regression | CI/CD pipeline gating | Behavioral |
| 4 | Async parallel race + consensus | Real-time model selection | Agreement |
| 5 | Ground-truth keyword scoring | Domain accuracy validation | Technical |
| 6 | Statistical consistency battery | Instruction-following evaluation | Variance |
