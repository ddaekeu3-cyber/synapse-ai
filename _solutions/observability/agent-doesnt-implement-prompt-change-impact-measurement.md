---
title: "Agent Doesn't Implement Prompt Change Impact Measurement"
description: "How to measure the impact of prompt changes on output quality, latency, cost, and behavior before rolling them out to production."
categories: [observability]
difficulty: intermediate
---

Changing a system prompt without measurement is flying blind. A small wording change can shift output format, reduce accuracy, or inflate token usage. Systematic impact measurement catches regressions before they reach users.

## Solution 1: Side-by-Side Output Comparison

Run both the old and new prompt on the same inputs and compare outputs structurally.

```python
import asyncio
from dataclasses import dataclass
from difflib import SequenceMatcher
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


@dataclass
class ComparisonResult:
    input_text: str
    old_output: str
    new_output: str
    similarity: float
    token_delta: int
    latency_delta_ms: float


def text_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


async def run_prompt(system: str, user: str) -> tuple[str, int, float]:
    import time
    start = time.monotonic()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    text = resp.content[0].text
    tokens = resp.usage.output_tokens
    return text, tokens, elapsed_ms


async def compare_prompts(
    old_system: str,
    new_system: str,
    test_inputs: list[str],
) -> list[ComparisonResult]:
    async def compare_one(user: str) -> ComparisonResult:
        (old_out, old_tok, old_lat), (new_out, new_tok, new_lat) = await asyncio.gather(
            run_prompt(old_system, user),
            run_prompt(new_system, user),
        )
        return ComparisonResult(
            input_text=user,
            old_output=old_out,
            new_output=new_out,
            similarity=text_similarity(old_out, new_out),
            token_delta=new_tok - old_tok,
            latency_delta_ms=new_lat - old_lat,
        )

    return list(await asyncio.gather(*[compare_one(inp) for inp in test_inputs]))


def print_report(results: list[ComparisonResult]):
    avg_sim = sum(r.similarity for r in results) / len(results)
    avg_tok_delta = sum(r.token_delta for r in results) / len(results)
    low_sim = [r for r in results if r.similarity < 0.80]

    print(f"=== Prompt Impact Report ===")
    print(f"  Inputs tested:       {len(results)}")
    print(f"  Avg similarity:      {avg_sim:.2%}")
    print(f"  Avg token delta:     {avg_tok_delta:+.1f}")
    print(f"  Low-similarity (<80%): {len(low_sim)}")
    for r in low_sim:
        print(f"    [{r.similarity:.0%}] {r.input_text[:60]!r}")


async def main():
    old_system = "You are a helpful assistant. Answer concisely."
    new_system = "You are a helpful assistant. Always answer in bullet points."

    inputs = [
        "What is machine learning?",
        "How does TCP/IP work?",
        "Explain recursion.",
    ]

    results = await compare_prompts(old_system, new_system, inputs)
    print_report(results)


asyncio.run(main())
```

## Solution 2: LLM-as-Judge Quality Delta

Use a judge model to score both old and new outputs on the same rubric and compute the quality delta.

```python
import asyncio
import json
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()
JUDGE_MODEL = "claude-sonnet-4-6"
TEST_MODEL = "claude-haiku-4-5-20251001"

RUBRIC = """
Score the following response on a scale of 1-10 for each criterion:
- accuracy: factual correctness
- clarity: ease of understanding
- completeness: covers the key points
- conciseness: no unnecessary verbosity

Respond with valid JSON only: {"accuracy": N, "clarity": N, "completeness": N, "conciseness": N}
"""


@dataclass
class QualityScores:
    accuracy: float
    clarity: float
    completeness: float
    conciseness: float

    @property
    def total(self) -> float:
        return (self.accuracy + self.clarity + self.completeness + self.conciseness) / 4


async def judge_response(question: str, response: str) -> QualityScores:
    resp = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=150,
        messages=[
            {
                "role": "user",
                "content": f"{RUBRIC}\n\nQuestion: {question}\n\nResponse:\n{response}",
            }
        ],
    )
    try:
        data = json.loads(resp.content[0].text)
        return QualityScores(**{k: float(v) for k, v in data.items()})
    except Exception:
        return QualityScores(5, 5, 5, 5)  # Neutral fallback


async def get_response(system: str, user: str) -> str:
    resp = await client.messages.create(
        model=TEST_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


async def measure_quality_delta(
    old_system: str, new_system: str, test_cases: list[str]
) -> dict:
    async def eval_one(q: str):
        old_resp, new_resp = await asyncio.gather(
            get_response(old_system, q),
            get_response(new_system, q),
        )
        old_scores, new_scores = await asyncio.gather(
            judge_response(q, old_resp),
            judge_response(q, new_resp),
        )
        return old_scores, new_scores

    all_scores = await asyncio.gather(*[eval_one(q) for q in test_cases])
    old_totals = [s[0].total for s in all_scores]
    new_totals = [s[1].total for s in all_scores]

    avg_old = sum(old_totals) / len(old_totals)
    avg_new = sum(new_totals) / len(new_totals)

    return {
        "old_avg_score": round(avg_old, 2),
        "new_avg_score": round(avg_new, 2),
        "delta": round(avg_new - avg_old, 2),
        "recommendation": "SHIP" if avg_new >= avg_old - 0.3 else "BLOCK",
    }


async def main():
    old = "You are a concise technical assistant."
    new = "You are a detailed technical assistant who always provides examples."

    questions = [
        "What is a hash table?",
        "Explain the CAP theorem.",
        "What is a race condition?",
    ]

    report = await measure_quality_delta(old, new, questions)
    print(json.dumps(report, indent=2))


asyncio.run(main())
```

## Solution 3: Token Cost and Latency Impact Profiler

Measure the cost and latency impact of a prompt change across a representative sample of production inputs.

```python
import asyncio
import statistics
import time
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"

# Haiku pricing (per million tokens)
INPUT_COST_PER_M = 0.80
OUTPUT_COST_PER_M = 4.00


@dataclass
class RunStats:
    input_tokens: list[int] = field(default_factory=list)
    output_tokens: list[int] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def avg_input_tokens(self) -> float:
        return statistics.mean(self.input_tokens) if self.input_tokens else 0

    @property
    def avg_output_tokens(self) -> float:
        return statistics.mean(self.output_tokens) if self.output_tokens else 0

    @property
    def p95_latency(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_lats = sorted(self.latencies_ms)
        idx = int(len(sorted_lats) * 0.95)
        return sorted_lats[min(idx, len(sorted_lats) - 1)]

    @property
    def cost_per_1k_requests(self) -> float:
        input_cost = (self.avg_input_tokens / 1_000_000) * INPUT_COST_PER_M * 1000
        output_cost = (self.avg_output_tokens / 1_000_000) * OUTPUT_COST_PER_M * 1000
        return round(input_cost + output_cost, 4)


async def profile_prompt(system: str, inputs: list[str]) -> RunStats:
    stats = RunStats()

    async def run_one(user: str):
        t0 = time.monotonic()
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        elapsed = (time.monotonic() - t0) * 1000
        stats.input_tokens.append(resp.usage.input_tokens)
        stats.output_tokens.append(resp.usage.output_tokens)
        stats.latencies_ms.append(elapsed)

    await asyncio.gather(*[run_one(inp) for inp in inputs])
    return stats


def print_cost_report(old: RunStats, new: RunStats):
    print("=== Cost & Latency Impact ===")
    print(f"{'Metric':<30} {'Old':>10} {'New':>10} {'Delta':>10}")
    print("-" * 62)

    metrics = [
        ("Avg input tokens", old.avg_input_tokens, new.avg_input_tokens),
        ("Avg output tokens", old.avg_output_tokens, new.avg_output_tokens),
        ("p95 latency (ms)", old.p95_latency, new.p95_latency),
        ("Cost per 1K reqs ($)", old.cost_per_1k_requests, new.cost_per_1k_requests),
    ]
    for name, o, n in metrics:
        delta = n - o
        sign = "+" if delta > 0 else ""
        print(f"{name:<30} {o:>10.2f} {n:>10.2f} {sign}{delta:>9.2f}")


async def main():
    old_system = "Answer the question concisely."
    new_system = (
        "Answer the question with a brief explanation, a concrete example, "
        "and a one-line summary."
    )

    inputs = [
        "What is caching?",
        "Explain load balancing.",
        "What is a deadlock?",
        "What are database indexes?",
        "What is idempotency?",
    ]

    old_stats, new_stats = await asyncio.gather(
        profile_prompt(old_system, inputs),
        profile_prompt(new_system, inputs),
    )
    print_cost_report(old_stats, new_stats)


asyncio.run(main())
```

## Solution 4: Behavioral Invariant Checker

Define invariants that should hold regardless of prompt changes, and flag any new prompt that violates them.

```python
import asyncio
import json
import re
from dataclasses import dataclass
from typing import Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


@dataclass
class Invariant:
    name: str
    check: Callable[[str, str], bool]
    description: str


INVARIANTS: list[Invariant] = [
    Invariant(
        "no_apology_prefix",
        lambda q, r: not r.lower().startswith(("i apologize", "i'm sorry", "sorry,")),
        "Response must not start with an apology",
    ),
    Invariant(
        "contains_answer",
        lambda q, r: len(r.strip()) > 20,
        "Response must be non-trivial (> 20 chars)",
    ),
    Invariant(
        "no_placeholder_brackets",
        lambda q, r: "[your" not in r.lower() and "[insert" not in r.lower(),
        "Response must not contain unfilled template placeholders",
    ),
    Invariant(
        "responds_in_english",
        lambda q, r: bool(re.search(r"[a-zA-Z]{3}", r)),
        "Response must contain English text",
    ),
]


async def get_response(system: str, user: str) -> str:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


async def check_invariants(
    system: str, test_cases: list[str]
) -> dict[str, list[str]]:
    """Returns mapping of invariant_name → list of failing inputs."""
    responses = await asyncio.gather(*[get_response(system, q) for q in test_cases])
    failures: dict[str, list[str]] = {inv.name: [] for inv in INVARIANTS}

    for q, r in zip(test_cases, responses):
        for inv in INVARIANTS:
            if not inv.check(q, r):
                failures[inv.name].append(q)

    return failures


async def diff_invariant_failures(old_system: str, new_system: str, test_cases: list[str]):
    old_failures, new_failures = await asyncio.gather(
        check_invariants(old_system, test_cases),
        check_invariants(new_system, test_cases),
    )

    print("=== Invariant Check ===")
    any_regression = False
    for inv in INVARIANTS:
        old_count = len(old_failures[inv.name])
        new_count = len(new_failures[inv.name])
        if new_count > old_count:
            print(f"[REGRESSION] {inv.name}: {old_count} → {new_count} failures")
            any_regression = True
        elif new_count < old_count:
            print(f"[IMPROVEMENT] {inv.name}: {old_count} → {new_count} failures")
        else:
            print(f"[STABLE] {inv.name}: {new_count} failures")

    print(f"\nVerdict: {'BLOCK (invariant regression)' if any_regression else 'PASS'}")


async def main():
    old = "You are a helpful assistant."
    new = "You are a helpful assistant. Always begin by saying 'I apologize for any confusion.'"

    inputs = [
        "What is 2+2?",
        "Explain gravity.",
        "What is Python?",
    ]

    await diff_invariant_failures(old, new, inputs)


asyncio.run(main())
```

## Solution 5: Canary Traffic Splitter with Metric Accumulator

Route a fraction of live traffic to the new prompt, accumulate metrics, and compare against the baseline.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from collections import defaultdict
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


@dataclass
class VariantMetrics:
    request_count: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    error_count: int = 0

    def record(self, tokens: int, latency: float, error: bool = False):
        self.request_count += 1
        self.total_output_tokens += tokens
        self.total_latency_ms += latency
        if error:
            self.error_count += 1

    @property
    def avg_tokens(self) -> float:
        return self.total_output_tokens / max(self.request_count, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.request_count, 1)

    @property
    def error_rate(self) -> float:
        return self.error_count / max(self.request_count, 1)


class CanaryRouter:
    def __init__(self, control_system: str, canary_system: str, canary_fraction: float = 0.10):
        self.variants = {"control": control_system, "canary": canary_system}
        self.canary_fraction = canary_fraction
        self.metrics: dict[str, VariantMetrics] = defaultdict(VariantMetrics)

    def _select_variant(self) -> str:
        return "canary" if random.random() < self.canary_fraction else "control"

    async def handle(self, user_message: str) -> str:
        variant = self._select_variant()
        system = self.variants[variant]
        t0 = time.monotonic()
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=300,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            elapsed = (time.monotonic() - t0) * 1000
            self.metrics[variant].record(resp.usage.output_tokens, elapsed)
            return resp.content[0].text
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            self.metrics[variant].record(0, elapsed, error=True)
            return f"Error: {e}"

    def report(self):
        print("=== Canary Report ===")
        for name, m in self.metrics.items():
            print(
                f"  [{name}] n={m.request_count} "
                f"avg_tokens={m.avg_tokens:.1f} "
                f"avg_latency={m.avg_latency_ms:.0f}ms "
                f"error_rate={m.error_rate:.1%}"
            )


async def main():
    router = CanaryRouter(
        control_system="Answer briefly.",
        canary_system="Answer with a one-sentence explanation and an example.",
        canary_fraction=0.30,
    )

    questions = [
        "What is a pointer?", "What is SQL?", "What is REST?",
        "What is a mutex?", "What is HTTP?", "What is JSON?",
        "What is a queue?", "What is a stack?", "What is hashing?",
        "What is OAuth?",
    ]

    await asyncio.gather(*[router.handle(q) for q in questions * 3])
    router.report()


asyncio.run(main())
```

## Solution 6: Automated Regression Report with Git Integration

On every prompt commit, run a regression suite and append the results to a Markdown report file.

```python
import asyncio
import json
import time
from pathlib import Path
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"
REPORT_PATH = Path("/tmp/prompt_regression_report.md")


@dataclass
class RegressionEntry:
    timestamp: str
    old_commit: str
    new_commit: str
    avg_similarity: float
    avg_token_delta: float
    quality_delta: float
    verdict: str


async def run_regression(
    old_system: str,
    new_system: str,
    test_inputs: list[str],
    old_commit: str = "old",
    new_commit: str = "new",
) -> RegressionEntry:
    from difflib import SequenceMatcher

    async def compare(user: str):
        old_r, new_r = await asyncio.gather(
            client.messages.create(
                model=MODEL, max_tokens=300, system=old_system,
                messages=[{"role": "user", "content": user}]
            ),
            client.messages.create(
                model=MODEL, max_tokens=300, system=new_system,
                messages=[{"role": "user", "content": user}]
            ),
        )
        sim = SequenceMatcher(None, old_r.content[0].text, new_r.content[0].text).ratio()
        tok_delta = new_r.usage.output_tokens - old_r.usage.output_tokens
        return sim, tok_delta

    results = await asyncio.gather(*[compare(inp) for inp in test_inputs])
    similarities = [r[0] for r in results]
    token_deltas = [r[1] for r in results]

    avg_sim = sum(similarities) / len(similarities)
    avg_tok = sum(token_deltas) / len(token_deltas)
    verdict = "PASS" if avg_sim >= 0.70 else "REVIEW"

    return RegressionEntry(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        old_commit=old_commit,
        new_commit=new_commit,
        avg_similarity=avg_sim,
        avg_token_delta=avg_tok,
        quality_delta=0.0,  # Extend with judge scoring
        verdict=verdict,
    )


def append_to_report(entry: RegressionEntry):
    row = (
        f"| {entry.timestamp} | `{entry.old_commit}` → `{entry.new_commit}` "
        f"| {entry.avg_similarity:.0%} | {entry.avg_token_delta:+.1f} "
        f"| **{entry.verdict}** |\n"
    )
    if not REPORT_PATH.exists():
        REPORT_PATH.write_text(
            "# Prompt Regression Report\n\n"
            "| Timestamp | Commits | Similarity | Token Δ | Verdict |\n"
            "|---|---|---|---|---|\n"
        )
    with REPORT_PATH.open("a") as f:
        f.write(row)
    print(f"[report] Appended entry: {entry.verdict} (similarity={entry.avg_similarity:.0%})")


async def main():
    old = "Answer in one sentence."
    new = "Answer in two sentences with an example."

    inputs = ["What is DNS?", "What is a CDN?", "What is TLS?"]
    entry = await run_regression(old, new, inputs, old_commit="abc123", new_commit="def456")
    append_to_report(entry)
    print(REPORT_PATH.read_text())


asyncio.run(main())
```

## Comparison

| Solution | Measurement type | LLM calls | Automation | Best for |
|---|---|---|---|---|
| **Side-by-side comparison** | Text similarity | 2× inputs | Full | Quick sanity check |
| **LLM-as-judge delta** | Quality scores | 4× inputs | Full | Accuracy-critical changes |
| **Cost & latency profiler** | Tokens + timing | 2× inputs | Full | Budget-conscious teams |
| **Invariant checker** | Pass/fail rules | 2× inputs | Full | Safety/format constraints |
| **Canary traffic splitter** | Live metrics | Production traffic | Semi | Gradual rollouts |
| **Git regression report** | Aggregate delta | 2× inputs | Full | CI/CD integration |

Start with **side-by-side comparison** (Solution 1) and **invariant checker** (Solution 4) together — one catches output drift, the other catches format regressions. Add **canary traffic splitter** (Solution 5) before any production rollout.
