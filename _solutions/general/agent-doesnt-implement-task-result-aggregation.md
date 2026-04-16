---
layout: solution
title: "Agent Doesn't Implement Task Result Aggregation"
category: general
description: "Collect, merge, and reduce results from parallel or sequential agent subtasks — combining partial outputs, handling failures without discarding successes, and producing a single coherent result from N independent task branches."
tags: [general, aggregation, parallel, multi-agent, fan-out, python]
---

# Agent Doesn't Implement Task Result Aggregation

Agents that run parallel subtasks but have no aggregation layer either return only the first result, lose partial successes when one branch fails, or rebuild aggregation logic redundantly across tasks. A dedicated aggregation step collects all outputs — including partial results — merges them according to a defined strategy, and feeds a single coherent summary back to the model.

## Option 1: Simple Parallel Gather with Partial Failure Tolerance

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def ask(question: str, label: str) -> dict:
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": question}],
        )
        return {"label": label, "result": resp.content[0].text, "ok": True}
    except Exception as e:
        return {"label": label, "result": None, "ok": False, "error": str(e)}

def aggregate_results(results: list[dict]) -> dict:
    """Merge results: collect successes, log failures, combine text."""
    ok      = [r for r in results if r["ok"]]
    failed  = [r for r in results if not r["ok"]]
    combined = "\n\n".join(f"[{r['label']}]\n{r['result']}" for r in ok)
    return {
        "combined": combined,
        "success_count": len(ok),
        "failure_count": len(failed),
        "failures": [{"label": r["label"], "error": r.get("error")} for r in failed],
    }

async def main():
    questions = [
        ("What is Python?",    "python"),
        ("What is asyncio?",   "asyncio"),
        ("What is FastAPI?",   "fastapi"),
        ("What is SQLite?",    "sqlite"),
    ]
    results = await asyncio.gather(*[ask(q, label) for q, label in questions])
    aggregated = aggregate_results(list(results))

    print(f"Success: {aggregated['success_count']}/{len(results)}")
    print(f"Failures: {aggregated['failure_count']}")
    print(f"\nCombined ({len(aggregated['combined'])} chars):")
    print(aggregated["combined"][:300])

    # Final synthesis call
    if aggregated["combined"]:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"Summarize these descriptions in 2 sentences:\n\n{aggregated['combined']}",
            }],
        )
        print(f"\nSynthesis: {resp.content[0].text[:150]}")

asyncio.run(main())

# Expected Token Savings: 4 parallel calls + 1 synthesis vs 4 sequential = ~3x faster; synthesis sees only combined outputs
# Environment: asyncio; return_exceptions=False in gather lets individual failures return error dicts
```

## Option 2: Weighted Aggregation by Confidence Score

```python
import anthropic
import asyncio
import json
import re

client = anthropic.AsyncAnthropic()

CONFIDENCE_SYSTEM = """Answer the question and rate your confidence as a JSON object:
{"answer": "...", "confidence": 0.0-1.0, "key_point": "one key insight"}
Confidence: 1.0=certain, 0.5=unsure, 0.0=guessing. Respond ONLY with valid JSON."""

async def scored_ask(question: str, context: str = "") -> dict:
    prompt = f"Context: {context}\n\nQuestion: {question}" if context else question
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=CONFIDENCE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {**data, "ok": True, "tokens": resp.usage.output_tokens}
    except Exception:
        pass
    return {"answer": "", "confidence": 0.0, "key_point": "", "ok": False, "tokens": 0}

def weighted_aggregate(scored_results: list[dict]) -> dict:
    """Weight answers by confidence; surface highest-confidence insights."""
    ok = [r for r in scored_results if r["ok"] and r["confidence"] > 0.3]
    if not ok:
        return {"answer": "No confident answers available.", "avg_confidence": 0.0}

    # Sort by confidence descending
    ok.sort(key=lambda r: r["confidence"], reverse=True)
    avg_conf = sum(r["confidence"] for r in ok) / len(ok)
    key_points = [r["key_point"] for r in ok if r["key_point"]]

    return {
        "top_answer":      ok[0]["answer"],
        "top_confidence":  ok[0]["confidence"],
        "avg_confidence":  round(avg_conf, 2),
        "key_points":      key_points[:3],
        "sources_used":    len(ok),
        "sources_rejected": len(scored_results) - len(ok),
    }

async def main():
    question = "What are the main benefits of Python's asyncio module?"
    contexts = [
        "Focus on I/O performance",
        "Focus on developer ergonomics",
        "Focus on ecosystem and libraries",
        "Focus on comparison with threads",
    ]
    results = await asyncio.gather(*[scored_ask(question, ctx) for ctx in contexts])
    agg = weighted_aggregate(list(results))

    print(f"Sources: {agg['sources_used']} used, {agg['sources_rejected']} rejected")
    print(f"Avg confidence: {agg['avg_confidence']}")
    print(f"Top answer ({agg['top_confidence']:.0%} confident): {agg['top_answer'][:100]}")
    print(f"Key points: {agg['key_points']}")

asyncio.run(main())

# Expected Token Savings: Low-confidence answers filtered before synthesis; reduces noise in final aggregation
# Environment: confidence threshold 0.3 is tunable; use higher for factual queries, lower for creative tasks
```

## Option 3: Map-Reduce Aggregation with SQLite Checkpoint

```python
import anthropic
import asyncio
import sqlite3
import json
import time
import uuid

client = anthropic.AsyncAnthropic()
DB = "task_aggregation.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS task_results (
            job_id TEXT, task_id TEXT, label TEXT,
            result TEXT, ok INTEGER, tokens INTEGER, ts REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS aggregations (
            job_id TEXT PRIMARY KEY, aggregated TEXT,
            task_count INTEGER, success_count INTEGER, ts REAL
        )
    """)
    con.commit(); con.close()

async def map_task(job_id: str, label: str, prompt: str) -> dict:
    task_id = str(uuid.uuid4())[:8]
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        result = resp.content[0].text
        ok, tokens = True, resp.usage.output_tokens
    except Exception as e:
        result, ok, tokens = str(e), False, 0

    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO task_results VALUES (?,?,?,?,?,?,?)",
        (job_id, task_id, label, result, int(ok), tokens, time.time()),
    )
    con.commit(); con.close()
    return {"task_id": task_id, "label": label, "result": result, "ok": ok}

async def reduce_results(job_id: str) -> dict:
    """Reduce: load all task results and synthesize."""
    init_db()
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT label, result, ok FROM task_results WHERE job_id=? AND ok=1",
        (job_id,),
    ).fetchall()
    con.close()

    if not rows:
        return {"aggregated": "No successful results.", "success_count": 0}

    combined = "\n\n".join(f"[{r[0]}]: {r[1][:200]}" for r in rows)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250,
        messages=[{
            "role": "user",
            "content": f"Synthesize these expert perspectives into one summary:\n\n{combined}",
        }],
    )
    aggregated = resp.content[0].text

    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO aggregations VALUES (?,?,?,?,?)",
        (job_id, aggregated, len(rows), len(rows), time.time()),
    )
    con.commit(); con.close()
    return {"aggregated": aggregated, "success_count": len(rows)}

async def main():
    init_db()
    job_id = str(uuid.uuid4())[:8]
    topic  = "Python asyncio"

    tasks = [
        (f"perspective_{i}", f"Describe {topic} from the perspective of a {role}.")
        for i, role in enumerate(["beginner", "senior engineer", "DevOps engineer", "data scientist"])
    ]

    print(f"Job {job_id}: mapping {len(tasks)} tasks")
    map_results = await asyncio.gather(*[map_task(job_id, label, prompt) for label, prompt in tasks])
    ok_count = sum(1 for r in map_results if r["ok"])
    print(f"Map: {ok_count}/{len(tasks)} succeeded")

    print("Reducing...")
    reduction = await reduce_results(job_id)
    print(f"Aggregated ({reduction['success_count']} sources):")
    print(reduction["aggregated"][:200])

asyncio.run(main())

# Expected Token Savings: SQLite checkpoint enables resume if reduce fails; synthesis sees only successful map outputs
# Environment: asyncio + SQLite; job_id links map outputs to reduce input; extend with retry on failed map tasks
```

## Option 4: Voting Aggregation for Factual Consistency

```python
import anthropic
import asyncio
import re
from collections import Counter

client = anthropic.AsyncAnthropic()

async def ask_for_answer(question: str, attempt: int) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"{question}\nRespond with a short, specific answer (1-3 sentences).",
        }],
    )
    return {"attempt": attempt, "answer": resp.content[0].text.strip()}

def normalize(text: str) -> str:
    """Normalize answer for comparison: lowercase, remove punctuation."""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()

def majority_vote(answers: list[dict]) -> dict:
    """Find the most common answer cluster by normalized prefix."""
    if not answers:
        return {"winner": "", "confidence": 0.0, "votes": 0, "total": 0}

    # Use first 40 chars of normalized answer as cluster key
    clusters: dict[str, list[str]] = {}
    for a in answers:
        key = normalize(a["answer"])[:40]
        clusters.setdefault(key, []).append(a["answer"])

    # Most common cluster
    winner_key = max(clusters, key=lambda k: len(clusters[k]))
    winner_answers = clusters[winner_key]
    votes = len(winner_answers)
    confidence = votes / len(answers)

    return {
        "winner":     winner_answers[0],  # representative answer
        "votes":      votes,
        "total":      len(answers),
        "confidence": round(confidence, 2),
        "clusters":   len(clusters),
    }

async def self_consistent_answer(question: str, n: int = 4) -> dict:
    """Self-consistency: ask N times, return majority answer."""
    answers = await asyncio.gather(*[ask_for_answer(question, i) for i in range(n)])
    vote = majority_vote(list(answers))
    print(f"  Votes: {vote['votes']}/{vote['total']} agree | {vote['clusters']} clusters")
    return vote

async def main():
    questions = [
        "Who created Python and in what year?",
        "What does the GIL stand for in Python?",
        "What is the default port for HTTP?",
    ]
    for q in questions:
        print(f"\nQ: {q}")
        result = await self_consistent_answer(q, n=4)
        print(f"A ({result['confidence']:.0%} confidence): {result['winner'][:80]}")

asyncio.run(main())

# Expected Token Savings: 4-shot self-consistency often cheaper than 1 Opus call; majority vote filters hallucinations
# Environment: n=3-5 for most questions; increase n for high-stakes factual queries; normalize is tunable
```

## Option 5: Hierarchical Aggregation — Chunk then Reduce

```python
import anthropic
import asyncio
import math

client = anthropic.AsyncAnthropic()

async def summarize_chunk(items: list[str], chunk_idx: int) -> dict:
    joined = "\n- " + "\n- ".join(items)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"Summarize these {len(items)} points into 2 sentences:{joined}",
        }],
    )
    return {
        "chunk": chunk_idx,
        "items": len(items),
        "summary": resp.content[0].text.strip(),
        "tokens": resp.usage.output_tokens,
    }

async def hierarchical_reduce(items: list[str], chunk_size: int = 5) -> str:
    """Reduce N items in O(log N) model calls via hierarchical aggregation."""
    current = items
    level = 0
    total_tokens = 0

    while len(current) > 1:
        # Split into chunks
        chunks = [current[i:i+chunk_size] for i in range(0, len(current), chunk_size)]
        if len(chunks) == 1 and len(chunks[0]) == 1:
            break

        print(f"  Level {level}: {len(current)} items -> {len(chunks)} chunks")
        chunk_results = await asyncio.gather(*[
            summarize_chunk(chunk, i) for i, chunk in enumerate(chunks)
        ])
        total_tokens += sum(r["tokens"] for r in chunk_results)
        current = [r["summary"] for r in chunk_results]
        level += 1

    # Final synthesis
    if len(current) > 1:
        final_resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": "Combine these summaries into one paragraph:\n\n" + "\n\n".join(current),
            }],
        )
        result = final_resp.content[0].text
        total_tokens += final_resp.usage.output_tokens
    else:
        result = current[0]

    print(f"  Total: {level} levels, {total_tokens} output tokens")
    return result

async def main():
    # Simulate 20 independent task results
    items = [
        f"Finding {i}: Python {'supports' if i % 2 == 0 else 'enables'} {'async' if i % 3 == 0 else 'type hints'} for better {'performance' if i % 4 == 0 else 'readability'}."
        for i in range(1, 21)
    ]
    print(f"Aggregating {len(items)} results hierarchically:")
    result = await hierarchical_reduce(items, chunk_size=4)
    print(f"\nFinal ({len(result)} chars): {result[:200]}")

asyncio.run(main())

# Expected Token Savings: O(log N) calls vs O(N) for sequential; 20 items = ~2 levels = ~6 calls vs 20 sequential
# Environment: asyncio; chunk_size=4-8 works well; reduce level count as chunk_size increases
```

## Option 6: Aggregation Pipeline with SQLite Dashboard

```python
import anthropic
import asyncio
import sqlite3
import time
import uuid

client = anthropic.AsyncAnthropic()
DB = "aggregation_pipeline.db"

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT, stage TEXT, label TEXT,
            result TEXT, ok INTEGER, tokens_in INTEGER, tokens_out INTEGER, ts REAL
        );
        CREATE TABLE IF NOT EXISTS pipeline_summary (
            run_id TEXT PRIMARY KEY, final_result TEXT,
            total_tasks INTEGER, successful INTEGER,
            total_tokens INTEGER, duration_ms REAL
        );
    """)
    con.commit(); con.close()

def log_stage(run_id: str, stage: str, label: str, result: str,
              ok: bool, tokens_in: int, tokens_out: int):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?)",
        (run_id, stage, label, result[:500], int(ok), tokens_in, tokens_out, time.time()),
    )
    con.commit(); con.close()

async def pipeline_task(run_id: str, label: str, prompt: str) -> dict:
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        result, ok = resp.content[0].text, True
        log_stage(run_id, "map", label, result, True,
                  resp.usage.input_tokens, resp.usage.output_tokens)
        return {"label": label, "result": result, "ok": True}
    except Exception as e:
        log_stage(run_id, "map", label, str(e), False, 0, 0)
        return {"label": label, "result": None, "ok": False}

async def pipeline_reduce(run_id: str, map_results: list[dict]) -> str:
    ok = [r for r in map_results if r["ok"]]
    combined = "\n\n".join(f"[{r['label']}]: {r['result'][:200]}" for r in ok)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250,
        messages=[{"role": "user",
                   "content": f"Aggregate these {len(ok)} responses into one answer:\n\n{combined}"}],
    )
    result = resp.content[0].text
    log_stage(run_id, "reduce", "aggregation", result, True,
              resp.usage.input_tokens, resp.usage.output_tokens)
    return result

async def run_pipeline(tasks: list[tuple[str, str]]) -> dict:
    init_db()
    run_id = str(uuid.uuid4())[:8]
    t0 = time.monotonic()

    map_results = await asyncio.gather(*[pipeline_task(run_id, label, prompt) for label, prompt in tasks])
    final = await pipeline_reduce(run_id, list(map_results))

    duration_ms = (time.monotonic() - t0) * 1000
    ok_count = sum(1 for r in map_results if r["ok"])

    con = sqlite3.connect(DB)
    total_tok = con.execute("SELECT SUM(tokens_in+tokens_out) FROM pipeline_runs WHERE run_id=?", (run_id,)).fetchone()[0]
    con.execute("INSERT OR REPLACE INTO pipeline_summary VALUES (?,?,?,?,?,?)",
                (run_id, final[:500], len(tasks), ok_count, total_tok or 0, duration_ms))
    con.commit(); con.close()

    return {"run_id": run_id, "result": final, "ok": ok_count, "total": len(tasks),
            "tokens": total_tok, "duration_ms": round(duration_ms, 1)}

async def main():
    tasks = [
        ("performance",  "What makes Python fast for I/O tasks?"),
        ("readability",  "Why is Python considered readable?"),
        ("ecosystem",    "What makes the Python ecosystem strong?"),
        ("typing",       "How do type hints help Python development?"),
    ]
    result = await run_pipeline(tasks)
    print(f"Run {result['run_id']}: {result['ok']}/{result['total']} ok | "
          f"{result['tokens']} tokens | {result['duration_ms']:.0f}ms")
    print(f"\nAggregated: {result['result'][:200]}")

asyncio.run(main())

# Expected Token Savings: SQLite dashboard shows per-run token costs; map+reduce pattern visible across all runs
# Environment: asyncio; extend pipeline_runs with retry logic; pipeline_summary supports cost-per-query analytics
```

## Comparison

| Option | Aggregation Strategy | Partial Failure Handling | Async | Persistence |
|--------|---------------------|------------------------|-------|-----------|
| 1 — Gather + Merge | Concatenate successes | Error dicts | Yes | No |
| 2 — Weighted by Confidence | Confidence-sorted | Threshold filter | Yes | No |
| 3 — Map-Reduce SQLite | LLM synthesis | SQLite checkpoint | Yes | SQLite |
| 4 — Majority Vote | Cluster + vote | Outlier rejection | Yes | No |
| 5 — Hierarchical Reduce | O(log N) LLM calls | Per-level | Yes | No |
| 6 — Pipeline Dashboard | LLM synthesis | Log failures | Yes | SQLite |
