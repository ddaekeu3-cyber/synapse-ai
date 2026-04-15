---
layout: solution
title: "Agent Doesn't Estimate Cost Before Long Batch Tasks"
category: token-cost
description: "Agent starts processing thousands of items without estimating total token spend, resulting in surprise bills and exhausted budgets mid-batch with no partial results saved."
tags: [token-cost, batch, budgeting, cost-control, production]
---

## Symptom

A batch job processes 50,000 documents overnight. In the morning the operator finds an unexpected invoice for $800, a half-finished output file, and an API quota exhaustion that stopped the run at item 23,471. There is no record of estimated cost before the job started, no mid-run budget checkpoint, and no alert that would have let the operator intervene before the bill arrived.

## Root Cause

Agents treat `client.messages.create()` as a zero-cost operation at planning time. Without a pre-flight estimate — based on average prompt size × item count × per-token price — there is no opportunity to confirm the budget, shrink the batch, or switch to a cheaper model before the spend is committed. Mid-run exhaustion leaves partial results that may be unusable without additional token spend to reconcile them.

## Fix

### Option 1 — Pre-flight cost estimate before batch start

```python
import anthropic

client = anthropic.Anthropic()

# Approximate prices per million tokens (update from Anthropic pricing page)
PRICES_PER_M_TOKENS = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

def estimate_batch_cost(
    item_count: int,
    avg_input_tokens: int,
    avg_output_tokens: int,
    model: str,
) -> dict:
    prices = PRICES_PER_M_TOKENS.get(model)
    if not prices:
        raise ValueError(f"Unknown model: {model}")
    input_cost  = (avg_input_tokens  * item_count / 1_000_000) * prices["input"]
    output_cost = (avg_output_tokens * item_count / 1_000_000) * prices["output"]
    return {
        "model":             model,
        "item_count":        item_count,
        "total_input_tok":   avg_input_tokens  * item_count,
        "total_output_tok":  avg_output_tokens * item_count,
        "estimated_usd":     round(input_cost + output_cost, 4),
        "input_cost_usd":    round(input_cost, 4),
        "output_cost_usd":   round(output_cost, 4),
    }

def run_batch_with_preflight(
    items: list[str],
    model: str = "claude-haiku-4-5-20251001",
    budget_usd: float = 5.0,
) -> list[str]:
    # Sample a few items to calibrate token counts
    sample = items[:min(3, len(items))]
    sample_tokens = []
    for s in sample:
        # tiktoken or anthropic tokenizer; here we use len/4 heuristic
        sample_tokens.append(len(s) // 4)
    avg_input = int(sum(sample_tokens) / len(sample_tokens)) + 100  # +100 for system prompt
    avg_output = 150  # conservative output estimate

    estimate = estimate_batch_cost(len(items), avg_input, avg_output, model)
    print(f"[preflight] estimated cost: ${estimate['estimated_usd']:.4f} USD")
    print(f"[preflight] {estimate['total_input_tok']:,} input + {estimate['total_output_tok']:,} output tokens")

    if estimate["estimated_usd"] > budget_usd:
        raise RuntimeError(
            f"Estimated cost ${estimate['estimated_usd']:.2f} exceeds budget ${budget_usd:.2f}. "
            f"Reduce items or switch to a cheaper model."
        )

    print(f"[preflight] budget OK — starting {len(items)}-item batch")
    results = []
    for item in items:
        response = client.messages.create(
            model=model,
            max_tokens=avg_output,
            messages=[{"role": "user", "content": item}],
        )
        results.append(response.content[0].text)
    return results

items = [f"Summarise document {i}." for i in range(100)]
try:
    results = run_batch_with_preflight(items, budget_usd=1.0)
    print(f"[batch] {len(results)} items processed")
except RuntimeError as e:
    print(f"[budget] {e}")
```

**Expected Token Savings:** Catches over-budget batches before a single token is spent; forces the operator to confirm or adjust before committing the full spend.
**Environment:** Offline batch processing; any agent job where item count and prompt size are known before execution starts.

---

### Option 2 — Sampled estimation: run 1% of items first

```python
import anthropic
import math

client = anthropic.Anthropic()

PRICES_PER_M = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}

def sample_and_estimate(
    items: list[str],
    model: str = "claude-haiku-4-5-20251001",
    sample_pct: float = 0.01,
    budget_usd: float = 10.0,
) -> tuple[list, dict]:
    """
    Run a small sample, measure actual token usage, extrapolate to full batch.
    Returns (sample_results, estimate_dict).
    """
    sample_size = max(1, math.ceil(len(items) * sample_pct))
    sample_items = items[:sample_size]

    print(f"[sample] running {sample_size}/{len(items)} items to calibrate cost")
    sample_results = []
    total_input = total_output = 0

    for item in sample_items:
        response = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": item}],
        )
        total_input  += response.usage.input_tokens
        total_output += response.usage.output_tokens
        sample_results.append(response.content[0].text)

    avg_in  = total_input  // sample_size
    avg_out = total_output // sample_size
    prices  = PRICES_PER_M[model]

    projected_in  = avg_in  * len(items)
    projected_out = avg_out * len(items)
    projected_usd = (projected_in / 1e6 * prices["input"] +
                     projected_out / 1e6 * prices["output"])

    estimate = {
        "sample_size":    sample_size,
        "avg_input_tok":  avg_in,
        "avg_output_tok": avg_out,
        "projected_usd":  round(projected_usd, 4),
        "within_budget":  projected_usd <= budget_usd,
    }
    print(f"[sample] projected cost: ${projected_usd:.4f} (budget: ${budget_usd:.2f})")
    return sample_results, estimate

def run_with_sampling(items: list[str], budget_usd: float = 5.0) -> list[str]:
    sample_results, est = sample_and_estimate(items, budget_usd=budget_usd)
    if not est["within_budget"]:
        raise RuntimeError(
            f"Projected ${est['projected_usd']:.2f} exceeds budget ${budget_usd:.2f}. "
            f"Reduce batch size or lower max_tokens."
        )
    print(f"[batch] proceeding with remaining {len(items) - est['sample_size']} items")
    remaining = items[est["sample_size"]:]
    full_results = list(sample_results)
    for item in remaining:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": item}],
        )
        full_results.append(resp.content[0].text)
    return full_results

items = [f"Classify sentiment of review {i}." for i in range(200)]
try:
    results = run_with_sampling(items, budget_usd=2.0)
    print(f"[done] {len(results)} items processed")
except RuntimeError as e:
    print(f"[budget] {e}")
```

**Expected Token Savings:** Sample results are reused — no wasted tokens on calibration calls; actual measured token counts are more accurate than heuristic estimates.
**Environment:** Variable-length items (e.g., user-generated content) where token count varies significantly across items.

---

### Option 3 — Running spend tracker with mid-batch abort

```python
import anthropic

client = anthropic.Anthropic()

PRICES_PER_M = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}

class SpendTracker:
    def __init__(self, budget_usd: float, model: str):
        self.budget_usd  = budget_usd
        self.model       = model
        self.spent_usd   = 0.0
        self.item_count  = 0
        self._prices     = PRICES_PER_M[model]

    def record(self, usage: anthropic.types.Usage) -> float:
        cost = (usage.input_tokens  / 1e6 * self._prices["input"] +
                usage.output_tokens / 1e6 * self._prices["output"])
        self.spent_usd  += cost
        self.item_count += 1
        return cost

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - self.spent_usd

    @property
    def over_budget(self) -> bool:
        return self.spent_usd >= self.budget_usd

    def status(self) -> str:
        pct = 100 * self.spent_usd / self.budget_usd if self.budget_usd else 0
        return (f"${self.spent_usd:.4f}/${self.budget_usd:.2f} "
                f"({pct:.1f}%) — {self.item_count} items")

def run_tracked_batch(
    items: list[str],
    model: str = "claude-haiku-4-5-20251001",
    budget_usd: float = 2.0,
    checkpoint_every: int = 10,
) -> tuple[list[str], dict]:
    tracker = SpendTracker(budget_usd, model)
    results = []

    for i, item in enumerate(items):
        if tracker.over_budget:
            print(f"[budget] limit reached after {tracker.item_count} items — stopping")
            break

        response = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": item}],
        )
        tracker.record(response.usage)
        results.append(response.content[0].text)

        if (i + 1) % checkpoint_every == 0:
            print(f"[tracker] {tracker.status()}")

    summary = {
        "processed": tracker.item_count,
        "total":     len(items),
        "spent_usd": round(tracker.spent_usd, 6),
        "complete":  tracker.item_count == len(items),
    }
    print(f"[batch] final: {summary}")
    return results, summary

items = [f"Translate item {i} to Spanish." for i in range(500)]
results, summary = run_tracked_batch(items, budget_usd=0.10)
```

**Expected Token Savings:** Mid-batch abort preserves the processed portion; results so far are still usable; operator can resume from the checkpoint item index without re-processing completed items.
**Environment:** Long overnight batch jobs; any pipeline where partial results have business value.

---

### Option 4 — Model selector: cheapest model within quality budget

```python
import anthropic

client = anthropic.Anthropic()

MODELS = [
    {"id": "claude-haiku-4-5-20251001", "input_per_m": 0.80,  "output_per_m": 4.00,  "quality": "fast"},
    {"id": "claude-sonnet-4-6",         "input_per_m": 3.00,  "output_per_m": 15.00, "quality": "balanced"},
    {"id": "claude-opus-4-6",           "input_per_m": 15.00, "output_per_m": 75.00, "quality": "best"},
]

def cheapest_model_within_budget(
    item_count: int,
    avg_input_tokens: int,
    avg_output_tokens: int,
    budget_usd: float,
    min_quality: str = "fast",
) -> dict | None:
    quality_order = ["fast", "balanced", "best"]
    min_q_idx = quality_order.index(min_quality)

    for model in MODELS:
        q_idx = quality_order.index(model["quality"])
        if q_idx < min_q_idx:
            continue
        cost = (avg_input_tokens  * item_count / 1e6 * model["input_per_m"] +
                avg_output_tokens * item_count / 1e6 * model["output_per_m"])
        if cost <= budget_usd:
            return {**model, "estimated_usd": round(cost, 4)}
    return None  # no model fits the budget

def run_with_auto_model(
    items: list[str],
    budget_usd: float = 5.0,
    min_quality: str = "fast",
) -> list[str]:
    avg_in  = max(len(s) // 4 for s in items[:5]) + 80
    avg_out = 150

    selected = cheapest_model_within_budget(len(items), avg_in, avg_out, budget_usd, min_quality)
    if not selected:
        raise RuntimeError(
            f"No model can process {len(items)} items within ${budget_usd:.2f}. "
            f"Reduce batch size or increase budget."
        )

    print(f"[model-select] using {selected['id']} "
          f"(est. ${selected['estimated_usd']:.2f} / budget ${budget_usd:.2f})")

    results = []
    for item in items:
        response = client.messages.create(
            model=selected["id"],
            max_tokens=avg_out,
            messages=[{"role": "user", "content": item}],
        )
        results.append(response.content[0].text)
    return results

items = [f"Extract keywords from document {i}." for i in range(1000)]
results = run_with_auto_model(items, budget_usd=2.00)
print(f"[done] {len(results)} items processed")
```

**Expected Token Savings:** Automatically routes to haiku when budget allows; avoids paying for opus/sonnet when a simpler model would suffice; budget enforcement happens before the first API call.
**Environment:** Multi-tenant SaaS agents where different customers have different budget tiers.

---

### Option 5 — Dry-run mode: count tokens without calling the API

```python
import anthropic

client = anthropic.Anthropic()

PRICES_PER_M = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}

def count_tokens(messages: list[dict], model: str) -> int:
    """Use Anthropic's token-counting endpoint (no charge, no generation)."""
    response = client.messages.count_tokens(
        model=model,
        messages=messages,
    )
    return response.input_tokens

def dry_run_estimate(
    items: list[str],
    model: str = "claude-haiku-4-5-20251001",
    expected_output_tokens: int = 150,
    sample_size: int = 5,
) -> dict:
    """
    Count tokens for a sample of items using the free count_tokens endpoint,
    then extrapolate to the full batch.
    """
    sample = items[:sample_size]
    token_counts = []
    for item in sample:
        msgs = [{"role": "user", "content": item}]
        toks = count_tokens(msgs, model)
        token_counts.append(toks)

    avg_input = int(sum(token_counts) / len(token_counts))
    prices    = PRICES_PER_M[model]
    total_in  = avg_input * len(items)
    total_out = expected_output_tokens * len(items)
    cost_usd  = total_in / 1e6 * prices["input"] + total_out / 1e6 * prices["output"]

    return {
        "item_count":       len(items),
        "avg_input_tokens": avg_input,
        "projected_input":  total_in,
        "projected_output": total_out,
        "estimated_usd":    round(cost_usd, 4),
        "model":            model,
    }

def run_with_dry_run(items: list[str], budget_usd: float = 5.0) -> list[str]:
    model = "claude-haiku-4-5-20251001"
    estimate = dry_run_estimate(items, model)
    print(f"[dry-run] {estimate}")

    if estimate["estimated_usd"] > budget_usd:
        raise RuntimeError(
            f"Dry-run estimate ${estimate['estimated_usd']:.4f} exceeds "
            f"budget ${budget_usd:.2f} — aborting."
        )

    results = []
    for item in items:
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": item}],
        )
        results.append(resp.content[0].text)
    return results

items = [f"Summarise report {i}." for i in range(300)]
try:
    results = run_with_dry_run(items, budget_usd=1.00)
    print(f"[done] {len(results)} items")
except RuntimeError as e:
    print(f"[budget] {e}")
```

**Expected Token Savings:** `count_tokens` is free and doesn't generate output; exact input-token counts eliminate heuristic estimation errors; prevents over-budget surprises with zero wasted generation tokens.
**Environment:** Agents using the Anthropic Python SDK ≥ 0.28; recommended for compliance environments that require documented cost approval before processing starts.

---

### Option 6 — Cost report after each batch with cumulative accounting

```python
import anthropic
import json
import os
import time

client = anthropic.Anthropic()

LEDGER_FILE = "/tmp/agent_cost_ledger.json"
PRICES_PER_M = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
}

def load_ledger() -> dict:
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"batches": [], "total_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0}

def save_ledger(ledger: dict) -> None:
    tmp = LEDGER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, LEDGER_FILE)

def record_batch(batch_id: str, model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICES_PER_M.get(model, {"input": 3.0, "output": 15.0})
    cost = input_tokens / 1e6 * prices["input"] + output_tokens / 1e6 * prices["output"]
    ledger = load_ledger()
    ledger["batches"].append({
        "batch_id":      batch_id,
        "model":         model,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "cost_usd":      round(cost, 6),
        "timestamp":     int(time.time()),
    })
    ledger["total_usd"]           = round(ledger["total_usd"] + cost, 6)
    ledger["total_input_tokens"]  += input_tokens
    ledger["total_output_tokens"] += output_tokens
    save_ledger(ledger)
    return cost

def run_and_report(items: list[str], batch_id: str, model: str = "claude-haiku-4-5-20251001") -> list[str]:
    results = []
    total_in = total_out = 0
    for item in items:
        resp = client.messages.create(
            model=model,
            max_tokens=150,
            messages=[{"role": "user", "content": item}],
        )
        total_in  += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        results.append(resp.content[0].text)

    batch_cost = record_batch(batch_id, model, total_in, total_out)
    ledger     = load_ledger()
    print(f"[ledger] batch '{batch_id}': ${batch_cost:.4f} | "
          f"cumulative: ${ledger['total_usd']:.4f} across {len(ledger['batches'])} batches")
    return results

run_and_report([f"Classify item {i}." for i in range(50)], batch_id="batch-A")
run_and_report([f"Tag document {i}." for i in range(30)],  batch_id="batch-B")
```

**Expected Token Savings:** Cumulative ledger surfaces cost trends across batches — identifies when token spend is creeping up before it becomes a problem; provides the audit trail needed for budget approval workflows.
**Environment:** Teams with monthly token budgets; multi-batch pipelines that run on a schedule; finance-auditable AI operations.

---

## Comparison

| Option | When Cost Is Checked | Extra API Calls | Accuracy | Mid-Batch Abort | Best For |
|---|---|---|---|---|---|
| 1. Pre-flight estimate | Before any item | 0 | Heuristic | No | Known item sizes; quick gate |
| 2. Sampled estimation | After 1% sample | sample_size | High (measured) | No | Variable item lengths |
| 3. Running tracker | After every item | 0 | Exact (post-hoc) | Yes | Long batches; partial results OK |
| 4. Model selector | Before any item | 0 | Heuristic | No | Multi-model pipelines; auto-routing |
| 5. Dry-run count_tokens | Before any item | sample_size | Exact (input only) | No | Compliance; zero-waste estimation |
| 6. Cost ledger | After each batch | 0 | Exact (post-hoc) | No | Budget tracking across many batches |
