---
layout: solution
title: "Agent Doesn't Implement Parallel Hypothesis Testing"
category: performance
description: "Test multiple hypotheses simultaneously in parallel rather than sequentially, dramatically cutting wall-clock time for diagnostic, debugging, and research tasks."
tags: [performance, parallel, hypothesis, testing, concurrency, latency]
---

# Agent Doesn't Implement Parallel Hypothesis Testing

When debugging or researching, agents typically explore one hypothesis at a time: test A, fail, test B, fail, test C, succeed. Sequential hypothesis testing multiplies latency by the number of hypotheses. Parallel hypothesis testing fans out all hypotheses simultaneously and returns as soon as any one succeeds — or aggregates all results for a more complete picture. For N hypotheses, wall-clock time drops from O(N) to O(1).

## Option 1: Fan-Out with First-Success Short-Circuit

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def test_hypothesis(hypothesis: str, context: str, agent_id: int) -> dict:
    """Test one hypothesis and return result with confidence."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Context: {context}\n\n"
                f"Hypothesis to test: {hypothesis}\n\n"
                "Evaluate this hypothesis. Respond with JSON: "
                '{"verdict": "supported" | "refuted" | "inconclusive", "confidence": 0-1, "reasoning": "<brief>"}'
            )
        }],
    )
    import json
    text = response.content[0].text.strip()
    try:
        data = json.loads(text)
        return {"hypothesis": hypothesis, "agent_id": agent_id, **data}
    except Exception:
        return {"hypothesis": hypothesis, "agent_id": agent_id, "verdict": "inconclusive", "confidence": 0.3, "reasoning": text[:100]}


async def parallel_hypothesis_test(
    context: str,
    hypotheses: list[str],
    stop_on_first_supported: bool = True,
    confidence_threshold: float = 0.80,
) -> list[dict]:
    """
    Test all hypotheses in parallel.
    If stop_on_first_supported=True, cancel remaining tasks as soon as one is confirmed.
    """
    results: list[dict] = []
    queue: asyncio.Queue = asyncio.Queue()

    async def run_and_enqueue(hypothesis: str, idx: int) -> None:
        result = await test_hypothesis(hypothesis, context, idx)
        await queue.put(result)

    tasks = [asyncio.create_task(run_and_enqueue(h, i)) for i, h in enumerate(hypotheses)]
    completed = 0

    while completed < len(hypotheses):
        result = await queue.get()
        results.append(result)
        completed += 1

        verdict = result.get("verdict")
        confidence = result.get("confidence", 0)
        print(f"[agent {result['agent_id']}] verdict={verdict} confidence={confidence:.2f} | {result['hypothesis'][:60]}")

        if stop_on_first_supported and verdict == "supported" and confidence >= confidence_threshold:
            print(f"\nFirst high-confidence hypothesis found! Cancelling {len(tasks) - completed} remaining tasks.")
            for task in tasks:
                task.cancel()
            break

    return results


async def main() -> None:
    context = "A Python web server that was handling 1000 rps suddenly dropped to 200 rps after a deployment."

    hypotheses = [
        "A new database query is missing an index, causing full table scans.",
        "A recently added logging statement is synchronous and blocking the event loop.",
        "Memory usage increased significantly, triggering frequent garbage collection.",
        "A third-party API dependency increased its response time.",
        "A configuration change reduced the number of worker processes.",
    ]

    print(f"Testing {len(hypotheses)} hypotheses in parallel...\n")
    results = await parallel_hypothesis_test(context, hypotheses, stop_on_first_supported=False)

    supported = [r for r in results if r.get("verdict") == "supported"]
    print(f"\n{'='*50}")
    print(f"Results: {len(supported)}/{len(results)} hypotheses supported")
    for r in sorted(results, key=lambda x: -x.get("confidence", 0)):
        print(f"  [{r['verdict']:12s}] conf={r.get('confidence', 0):.2f} | {r['hypothesis'][:60]}")


asyncio.run(main())

# Expected Token Savings: N/A; wall-clock time drops from 5x to 1x; all 5 hypotheses tested in parallel
# Environment: Python 3.11+; stop_on_first_supported=True for debugging (short-circuit); False for research (full picture)
```

## Option 2: Hypothesis Tournament with Bracket Elimination

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def score_hypothesis(hypothesis: str, context: str) -> tuple[str, float]:
    """Score a single hypothesis 0-10 for likelihood."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"Given: {context}\n\n"
                f"How likely is this hypothesis to be correct? Score 0-10.\n"
                f"Hypothesis: {hypothesis}\n\n"
                "Respond with JSON: {\"score\": <number>, \"one_line_reason\": \"...\"}"
            )
        }],
    )
    import json
    try:
        data = json.loads(response.content[0].text.strip())
        return hypothesis, float(data.get("score", 5.0))
    except Exception:
        return hypothesis, 5.0


async def tournament_round(hypotheses: list[str], context: str) -> list[tuple[str, float]]:
    """Score all hypotheses in this round in parallel."""
    tasks = [asyncio.create_task(score_hypothesis(h, context)) for h in hypotheses]
    scored = await asyncio.gather(*tasks)
    return sorted(scored, key=lambda x: -x[1])


async def hypothesis_tournament(context: str, hypotheses: list[str], rounds: int = 2) -> str:
    """
    Multi-round tournament: each round scores hypotheses in parallel,
    then eliminates the bottom half.
    """
    current = list(hypotheses)
    print(f"Starting tournament with {len(current)} hypotheses, {rounds} rounds\n")

    for round_num in range(1, rounds + 1):
        print(f"--- Round {round_num} ({len(current)} hypotheses) ---")
        scored = await tournament_round(current, context)

        for h, score in scored:
            print(f"  score={score:.1f} | {h[:70]}")

        # Keep top half (minimum 1)
        keep = max(1, len(scored) // 2)
        current = [h for h, _ in scored[:keep]]
        print(f"Advancing {keep} hypothesis/hypotheses to next round\n")

        if len(current) == 1:
            break

    winner = current[0]
    print(f"WINNER: {winner}")
    return winner


async def main() -> None:
    context = "An ML training job that previously took 2 hours now takes 8 hours."
    hypotheses = [
        "GPU utilization dropped due to a data loading bottleneck.",
        "Batch size was accidentally reduced by a config change.",
        "A new regularization term added 3x computational overhead.",
        "The dataset grew 4x but pipeline wasn't scaled accordingly.",
        "A distributed training node is failing silently and being retried.",
        "Mixed-precision training was accidentally disabled.",
    ]
    await hypothesis_tournament(context, hypotheses, rounds=2)


asyncio.run(main())

# Expected Token Savings: 50% per elimination round; bracket cuts token cost while improving accuracy through ranking
# Environment: Python 3.11+; use 2-3 rounds; more rounds add latency without much accuracy gain
```

## Option 3: Evidence-Weighted Hypothesis Aggregation

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class HypothesisResult:
    hypothesis: str
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    score: float = 0.0
    tested_by: int = 0


async def gather_evidence(hypothesis: str, context: str, evidence_type: str) -> list[str]:
    """Gather supporting or refuting evidence for a hypothesis."""
    prompt = (
        f"Context: {context}\n\nHypothesis: {hypothesis}\n\n"
        f"List 2-3 pieces of {evidence_type} evidence for this hypothesis. "
        "Respond with JSON: {\"evidence\": [\"...\", \"...\"]}"
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(response.content[0].text.strip())
        return data.get("evidence", [])
    except Exception:
        return []


async def evaluate_hypothesis_fully(
    hypothesis: str, context: str, agent_id: int
) -> HypothesisResult:
    """Gather evidence for and against in parallel, then compute a score."""
    result = HypothesisResult(hypothesis=hypothesis, tested_by=agent_id)

    # Evidence gathering runs in parallel
    for_evidence, against_evidence = await asyncio.gather(
        gather_evidence(hypothesis, context, "supporting"),
        gather_evidence(hypothesis, context, "refuting"),
    )

    result.evidence_for = for_evidence
    result.evidence_against = against_evidence
    # Simple score: (supporting - refuting) normalized
    result.score = (len(for_evidence) - len(against_evidence)) / max(len(for_evidence) + len(against_evidence), 1)

    print(f"[agent {agent_id}] {hypothesis[:50]}... score={result.score:.2f}")
    return result


async def parallel_evidence_aggregation(context: str, hypotheses: list[str]) -> list[HypothesisResult]:
    """Test all hypotheses fully in parallel with evidence aggregation."""
    tasks = [
        asyncio.create_task(evaluate_hypothesis_fully(h, context, i))
        for i, h in enumerate(hypotheses)
    ]
    results: list[HypothesisResult] = await asyncio.gather(*tasks)
    return sorted(results, key=lambda r: -r.score)


async def run_agent_with_parallel_testing(question: str, hypotheses: list[str]) -> str:
    print(f"Testing {len(hypotheses)} hypotheses in parallel with evidence gathering...\n")
    results = await parallel_evidence_aggregation(question, hypotheses)

    # Summarize findings for the agent
    summary = "\n\n".join(
        f"Hypothesis: {r.hypothesis}\n"
        f"Score: {r.score:.2f}\n"
        f"Evidence for: {r.evidence_for[:2]}\n"
        f"Evidence against: {r.evidence_against[:2]}"
        for r in results[:3]
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Based on this hypothesis evaluation:\n\n{summary}\n\nWhat is the most likely explanation?"
        }],
    )
    return response.content[0].text


async def main() -> None:
    context = "API error rate went from 0.1% to 12% after a Friday deployment."
    hypotheses = [
        "A new validation rule is rejecting previously valid inputs.",
        "A database connection pool is exhausted under load.",
        "A bug in the error handling is surfacing previously silent failures.",
        "A third-party service dependency is experiencing an outage.",
    ]
    answer = await run_agent_with_parallel_testing(context, hypotheses)
    print(f"\nConclusion: {answer}")


asyncio.run(main())

# Expected Token Savings: Evidence gathering within each hypothesis is also parallel (2x speedup per hypothesis)
# Environment: Python 3.11+; evidence quality improves with sonnet; haiku is sufficient for initial triage
```

## Option 4: Timed Race with Best-Answer Collection

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

RACE_TIMEOUT = 15.0   # Max seconds to wait for all hypotheses
MIN_ANSWERS = 2       # Minimum answers before returning early if time runs out


async def test_with_timing(hypothesis: str, context: str, idx: int) -> dict:
    start = time.monotonic()
    import json
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Context: {context}\n\nIs this hypothesis plausible? {hypothesis}\n\n"
                'JSON: {"verdict": "yes"|"no"|"maybe", "confidence": 0-1, "key_reason": "..."}'
            )
        }],
    )
    elapsed = time.monotonic() - start
    try:
        data = json.loads(response.content[0].text.strip())
        return {"idx": idx, "hypothesis": hypothesis, "elapsed": elapsed, **data}
    except Exception:
        return {"idx": idx, "hypothesis": hypothesis, "elapsed": elapsed, "verdict": "maybe", "confidence": 0.5, "key_reason": "parse error"}


async def timed_race(context: str, hypotheses: list[str]) -> list[dict]:
    """
    Race all hypothesis tests against a timeout.
    Collect results as they arrive; return after RACE_TIMEOUT or all done.
    """
    queue: asyncio.Queue = asyncio.Queue()
    results: list[dict] = []
    start = time.monotonic()

    async def run_task(h: str, idx: int) -> None:
        result = await test_with_timing(h, context, idx)
        await queue.put(result)

    tasks = [asyncio.create_task(run_task(h, i)) for i, h in enumerate(hypotheses)]

    while len(results) < len(hypotheses):
        remaining_time = RACE_TIMEOUT - (time.monotonic() - start)
        if remaining_time <= 0:
            if len(results) >= MIN_ANSWERS:
                print(f"[timeout] Collected {len(results)}/{len(hypotheses)} results within {RACE_TIMEOUT}s")
                break
            # Wait a bit more if we haven't hit minimum
            remaining_time = 2.0

        try:
            result = await asyncio.wait_for(queue.get(), timeout=remaining_time)
            results.append(result)
            elapsed = result.get("elapsed", 0)
            print(f"[{elapsed:.2f}s] #{result['idx']} verdict={result['verdict']} conf={result.get('confidence', 0):.2f}")
        except asyncio.TimeoutError:
            if len(results) >= MIN_ANSWERS:
                print(f"[timeout] Using {len(results)} results collected so far")
                break

    for task in tasks:
        task.cancel()

    total_time = time.monotonic() - start
    print(f"\nCompleted in {total_time:.2f}s (sequential would take ~{len(hypotheses) * 2.0:.0f}s)")
    return results


async def main() -> None:
    context = "After upgrading a Python service to 3.12, CPU usage doubled."

    hypotheses = [
        "The new version has different GC behavior under load.",
        "A dependency is calling a deprecated fast path that's now slower.",
        "asyncio internals changed and existing code uses more CPU.",
        "The new version enables stricter type checking at runtime.",
        "A performance-critical C extension is not yet optimized for 3.12.",
    ]

    results = await timed_race(context, hypotheses)
    supported = sorted([r for r in results if r.get("verdict") == "yes"], key=lambda x: -x.get("confidence", 0))
    print(f"\nTop hypotheses: {[r['hypothesis'][:60] for r in supported[:2]]}")


asyncio.run(main())

# Expected Token Savings: N/A; timed race guarantees bounded latency regardless of slow hypothesis evaluations
# Environment: Python 3.11+; RACE_TIMEOUT should be 2-3x the expected single-call latency
```

## Option 5: Stratified Parallel Testing with Priority Lanes

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class Hypothesis:
    text: str
    priority: int = 5   # 1 (highest) to 10 (lowest)
    tags: list[str] = None  # e.g., ["configuration", "database", "network"]

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []


async def test_hypothesis(h: Hypothesis, context: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"Context: {context}\nHypothesis: {h.text}\nScore 1-10 for likelihood. JSON: {{\"score\": N, \"brief\": \"...\"}}"
        }],
    )
    try:
        data = json.loads(response.content[0].text.strip())
        return {"hypothesis": h.text, "priority": h.priority, "tags": h.tags, **data}
    except Exception:
        return {"hypothesis": h.text, "priority": h.priority, "tags": h.tags, "score": 5, "brief": "parse error"}


async def stratified_parallel_test(context: str, hypotheses: list[Hypothesis]) -> list[dict]:
    """
    Run high-priority hypotheses first, then medium, then low — all in parallel within each tier.
    If a high-priority hypothesis scores >= 8, skip lower-priority tiers.
    """
    tiers = {
        "high":   [h for h in hypotheses if h.priority <= 3],
        "medium": [h for h in hypotheses if 3 < h.priority <= 7],
        "low":    [h for h in hypotheses if h.priority > 7],
    }

    all_results = []

    for tier_name, tier_hyps in tiers.items():
        if not tier_hyps:
            continue

        print(f"\n[tier: {tier_name}] Testing {len(tier_hyps)} hypotheses in parallel...")
        tasks = [asyncio.create_task(test_hypothesis(h, context)) for h in tier_hyps]
        tier_results = await asyncio.gather(*tasks)

        for r in tier_results:
            print(f"  score={r.get('score', 0):4.1f} | prio={r['priority']} | {r['hypothesis'][:60]}")

        all_results.extend(tier_results)

        # Short-circuit: if high-priority hypothesis is very likely, skip lower tiers
        if tier_name == "high":
            best_score = max((r.get("score", 0) for r in tier_results), default=0)
            if best_score >= 8:
                print(f"\n[short-circuit] High-priority hypothesis scored {best_score}/10 — skipping lower tiers")
                break

    return sorted(all_results, key=lambda r: -r.get("score", 0))


async def main() -> None:
    context = "A cron job that sends daily reports stopped executing silently 3 days ago."

    hypotheses = [
        Hypothesis("The cron job schedule was accidentally deleted.", priority=1, tags=["configuration"]),
        Hypothesis("A permission change prevents the job from writing to the output directory.", priority=2, tags=["permissions"]),
        Hypothesis("The script crashes silently due to an unhandled exception.", priority=3, tags=["code"]),
        Hypothesis("The server running the cron job was replaced and jobs weren't migrated.", priority=4, tags=["infrastructure"]),
        Hypothesis("A dependency (Python package) was updated and broke the script.", priority=5, tags=["dependencies"]),
        Hypothesis("Network connectivity to the SMTP server was lost.", priority=7, tags=["network"]),
        Hypothesis("The report template changed in a way that causes a rendering error.", priority=9, tags=["data"]),
    ]

    results = await stratified_parallel_test(context, hypotheses)
    print(f"\nTop findings:")
    for r in results[:3]:
        print(f"  score={r.get('score', 0):.1f} | {r['hypothesis'][:70]}")


asyncio.run(main())

# Expected Token Savings: Short-circuit skips low-priority tiers when high-priority answer is found early
# Environment: Python 3.11+; assign priority based on historical frequency and blast radius of each hypothesis class
```

## Option 6: Hypothesis Tree with Recursive Parallel Expansion

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

MAX_DEPTH = 2
BRANCH_FACTOR = 3   # Sub-hypotheses per parent
PRUNE_THRESHOLD = 4  # Don't expand hypotheses scoring below this


@dataclass
class HypothesisNode:
    hypothesis: str
    depth: int = 0
    score: float = 0.0
    children: list["HypothesisNode"] = field(default_factory=list)
    reasoning: str = ""


async def score_and_expand(node: HypothesisNode, context: str) -> None:
    """Score a hypothesis and optionally expand into sub-hypotheses."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Context: {context}\n\nHypothesis: {node.hypothesis}\n\n"
                f"Score 1-10 and list {BRANCH_FACTOR} more specific sub-hypotheses if score >= {PRUNE_THRESHOLD}.\n"
                f'JSON: {{"score": N, "reasoning": "...", "sub_hypotheses": ["...", "...", "..."]}}'
            )
        }],
    )
    try:
        data = json.loads(response.content[0].text.strip())
        node.score = float(data.get("score", 5))
        node.reasoning = data.get("reasoning", "")

        indent = "  " * node.depth
        print(f"{indent}[depth {node.depth}] score={node.score:.1f} | {node.hypothesis[:60]}")

        # Expand if promising and not at max depth
        if node.score >= PRUNE_THRESHOLD and node.depth < MAX_DEPTH:
            sub_hyps = data.get("sub_hypotheses", [])[:BRANCH_FACTOR]
            node.children = [HypothesisNode(h, depth=node.depth + 1) for h in sub_hyps]
            # Recurse in parallel
            await asyncio.gather(*[score_and_expand(child, context) for child in node.children])
    except Exception as e:
        node.score = 5.0
        node.reasoning = f"parse error: {e}"


def collect_leaves(node: HypothesisNode) -> list[HypothesisNode]:
    """Collect all leaf nodes (deepest explored hypotheses)."""
    if not node.children:
        return [node]
    leaves = []
    for child in node.children:
        leaves.extend(collect_leaves(child))
    return leaves


async def hypothesis_tree_search(context: str, root_hypotheses: list[str]) -> list[HypothesisNode]:
    """Build and search a hypothesis tree in parallel."""
    roots = [HypothesisNode(h, depth=0) for h in root_hypotheses]

    print(f"Exploring hypothesis tree: {len(roots)} roots, depth {MAX_DEPTH}, branch {BRANCH_FACTOR}\n")
    await asyncio.gather(*[score_and_expand(root, context) for root in roots])

    # Collect all leaf findings, sorted by score
    all_leaves = []
    for root in roots:
        all_leaves.extend(collect_leaves(root))

    return sorted(all_leaves, key=lambda n: -n.score)


async def main() -> None:
    context = "A microservice started returning 503 errors for 15% of requests after a Kubernetes deployment."

    root_hypotheses = [
        "The new deployment has insufficient resources (CPU/memory).",
        "A downstream dependency is overwhelmed by increased traffic.",
        "The health check configuration changed and pods are being killed prematurely.",
    ]

    leaves = await hypothesis_tree_search(context, root_hypotheses)

    print(f"\n\nTop {min(5, len(leaves))} findings:")
    for leaf in leaves[:5]:
        print(f"  score={leaf.score:.1f} depth={leaf.depth} | {leaf.hypothesis[:70]}")


asyncio.run(main())

# Expected Token Savings: Tree pruning skips low-score branches — saves 30-50% vs full expansion
# Environment: Python 3.11+; MAX_DEPTH=2 with BRANCH_FACTOR=3 explores up to 12 hypotheses; tune for your latency budget
```

## Comparison

| Option | Strategy | Short-Circuit | Prioritized | Tree Search | Best For |
|--------|----------|--------------|-------------|-------------|----------|
| 1. Fan-Out | All parallel | Yes (first supported) | No | No | Debugging with definitive answer |
| 2. Tournament | Bracket elimination | No | Via ranking | No | Large hypothesis sets (10+) |
| 3. Evidence Aggregation | All parallel with evidence | No | No | No | Research with nuanced analysis |
| 4. Timed Race | Bounded time collection | Via timeout | No | No | SLA-bound diagnostic tasks |
| 5. Stratified | Priority tier lanes | Yes (high-tier) | Yes | No | Mixed-priority hypothesis sets |
| 6. Tree Search | Recursive expansion | Via pruning | No | Yes | Deep root cause analysis |
