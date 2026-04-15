---
layout: solution
title: "Agent Doesn't Summarise Completed Subtask Results Before Continuing"
category: context-window
description: "Agent accumulates full tool outputs and subtask responses in context instead of compressing completed work into summaries, filling the context window prematurely."
tags: [context-window, summarisation, memory, efficiency, token-cost, long-running]
---

## Symptom

Context fills up with verbose completed subtask outputs:

```
[Turn 1]  user: Analyse our Q1-Q4 sales data and write a report
[Turn 2]  tool: read_file("q1_sales.csv") → 8,000 tokens of raw CSV data
[Turn 3]  tool: read_file("q2_sales.csv") → 7,500 tokens of raw CSV data
[Turn 4]  tool: read_file("q3_sales.csv") → 9,000 tokens of raw CSV data
[Turn 5]  tool: read_file("q4_sales.csv") → 8,500 tokens of raw CSV data

Context now: 33,000 tokens of raw CSV — model hasn't started writing the report yet
Available context for remaining work: ~60K - 33K = 27K tokens

# Model hits context limit before finishing the report
# Or pays 33K tokens per turn for the rest of the conversation
```

Each completed subtask leaves its full output in context even after the data has been processed. The context grows without bound through the task lifetime.

## Root Cause

LLM context is append-only during a conversation. Tool results and assistant messages are never automatically compressed. A long-running task that reads many files, calls many APIs, or iterates many times accumulates all intermediate outputs. By the time the final synthesis step begins, the model is paying tokens for data from step 1 that is no longer needed in raw form.

## Fix

---

### Option 1: Explicit Summarise-After-Each-Subtask Pattern

After each subtask completes, ask the model to write a compact summary and discard the raw output from context.

```python
import anthropic

client = anthropic.Anthropic()

def summarise_result(task_name: str, raw_result: str, max_summary_tokens: int = 150) -> str:
    """Compress a completed subtask result into a brief summary."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_summary_tokens,
        system="Summarise the key facts from this data in 2-5 bullet points. Be precise with numbers.",
        messages=[{
            "role": "user",
            "content": f"Data from {task_name}:\n{raw_result[:4000]}"  # cap input too
        }],
    )
    return f"[Summary of {task_name}]\n{response.content[0].text}"

def run_multi_step_analysis(quarters: list[str]) -> str:
    # Simulate quarterly data (in practice: read files, call APIs, etc.)
    raw_data = {
        "Q1": "Jan: $1.2M (+5%), Feb: $1.1M (-8%), Mar: $1.4M (+27%). Top product: Widget A ($450K). Churn: 3.2%.",
        "Q2": "Apr: $1.5M (+7%), May: $1.6M (+7%), Jun: $1.3M (-19%). Top product: Widget B ($520K). Churn: 2.8%.",
        "Q3": "Jul: $1.7M (+31%), Aug: $1.8M (+6%), Sep: $1.6M (-11%). Top product: Widget A ($610K). Churn: 3.5%.",
        "Q4": "Oct: $2.0M (+25%), Nov: $2.3M (+15%), Dec: $2.5M (+9%). Top product: Widget C ($890K). Churn: 2.1%.",
    }

    # Collect summaries instead of raw data
    summaries: list[str] = []
    for quarter in quarters:
        raw = raw_data.get(quarter, "No data")
        # Raw data: ~50 tokens per quarter × 4 = 200 tokens
        # Summary: ~60 tokens per quarter × 4 = 240 tokens (similar, but scales better for real data)
        summary = summarise_result(f"{quarter} Sales", raw, max_summary_tokens=100)
        summaries.append(summary)
        print(f"Summarised {quarter}: {len(summary)} chars")

    # Final synthesis uses only summaries, not raw data
    combined_context = "\n\n".join(summaries)
    synthesis = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a business analyst. Write a concise annual sales report.",
        messages=[{
            "role": "user",
            "content": f"Quarterly summaries:\n{combined_context}\n\nWrite an executive summary of annual performance."
        }],
    )
    return synthesis.content[0].text

result = run_multi_step_analysis(["Q1", "Q2", "Q3", "Q4"])
print(result)
```

**Expected Token Savings:** Raw CSV for 4 quarters: ~33,000 tokens. Summaries: ~600 tokens total. Savings: 98% reduction in context for completed subtask data. For a 200K token context limit, this extends the effective task length by 50×.
**Environment:** Works for any subtask that produces large raw outputs (files, API responses, search results). Summary quality depends on `claude-haiku-4-5-20251001` — validate that key metrics are preserved.

---

### Option 2: Rolling Context Compaction — Compress Oldest Turns Progressively

Instead of summarising after each step, monitor context size and compress the oldest N turns whenever a threshold is crossed.

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class CompactingContext:
    messages: list[dict] = field(default_factory=list)
    compaction_threshold: int = 40_000   # tokens before compacting
    target_size: int = 20_000            # target tokens after compaction
    _client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)

    def _estimate_tokens(self) -> int:
        """Rough estimate: 1 token ≈ 4 chars."""
        total_chars = sum(
            len(str(m.get("content", ""))) for m in self.messages
        )
        return total_chars // 4

    def _compact_oldest(self, n_messages: int) -> None:
        """Summarise the oldest n_messages and replace with a single summary."""
        if len(self.messages) < n_messages + 2:  # keep at least 2 messages
            return

        to_compact = self.messages[:n_messages]
        self.messages = self.messages[n_messages:]

        # Summarise compacted messages
        conversation_text = "\n".join(
            f"{m['role'].upper()}: {str(m.get('content', ''))[:500]}"
            for m in to_compact
        )
        summary_response = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="Summarise this conversation excerpt, preserving all key facts, decisions, and data points.",
            messages=[{"role": "user", "content": conversation_text}],
        )
        summary = summary_response.content[0].text

        # Inject summary as a system-style note at the start
        self.messages.insert(0, {
            "role": "user",
            "content": f"[CONTEXT SUMMARY — earlier conversation]\n{summary}",
        })
        self.messages.insert(1, {
            "role": "assistant",
            "content": "Understood. I have the context from the earlier conversation.",
        })

        print(f"Compacted {n_messages} messages → summary ({len(summary)} chars)")

    def maybe_compact(self) -> bool:
        """Compact if over threshold. Returns True if compaction occurred."""
        estimated = self._estimate_tokens()
        if estimated > self.compaction_threshold:
            # Compact oldest 40% of messages
            n = max(2, len(self.messages) * 4 // 10)
            self._compact_oldest(n)
            return True
        return False

    def append(self, message: dict) -> None:
        self.messages.append(message)
        self.maybe_compact()

def run_long_task_with_compaction(steps: list[str]) -> str:
    ctx = CompactingContext(compaction_threshold=8_000, target_size=4_000)
    client = anthropic.Anthropic()

    ctx.append({"role": "user", "content": "Process all the following data sources: " + ", ".join(steps)})

    for step in steps:
        # Simulate large tool result
        large_result = f"Data from {step}: " + "x" * 500  # 500 chars ≈ 125 tokens

        ctx.append({"role": "assistant", "content": f"Processing {step}..."})
        ctx.append({"role": "user", "content": f"[Tool result for {step}]\n{large_result}"})

        print(f"After {step}: ~{ctx._estimate_tokens()} tokens")

    # Final synthesis
    ctx.append({"role": "user", "content": "Provide a final summary of all processed data."})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=ctx.messages,
    )
    return response.content[0].text

result = run_long_task_with_compaction([f"source_{i}" for i in range(20)])
print(result[:200])
```

**Expected Token Savings:** Compaction fires when context exceeds 40K tokens, reducing to ~20K. For a 50-step task that would otherwise hit 100K tokens, compaction reduces peak context to ~25K — 75% reduction, enabling tasks that would otherwise fail with context limit errors.
**Environment:** Compaction threshold should be ~60-70% of the model's context limit. Each compaction costs one cheap model call (~300 tokens output). Net savings become positive after ~5 compactions.

---

### Option 3: Structured Result Accumulator — Replace Raw Results with Typed Extracts

Define a structured schema for each subtask type and extract only schema-relevant fields immediately after each tool call, discarding the raw response.

```python
import json
import anthropic
from pydantic import BaseModel

class SalesMetrics(BaseModel):
    period: str
    total_revenue: float
    growth_pct: float
    top_product: str
    top_product_revenue: float
    churn_rate: float

class SearchResult(BaseModel):
    url: str
    title: str
    key_finding: str
    relevance_score: float

def extract_structured(raw_result: str, schema: type[BaseModel], task_name: str) -> BaseModel | None:
    """Use model to extract structured data from raw result."""
    client = anthropic.Anthropic()

    schema_json = schema.model_json_schema()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Extract data matching this JSON schema from the input. Return only valid JSON.\nSchema: {json.dumps(schema_json)}",
        messages=[{"role": "user", "content": f"Source: {task_name}\nData: {raw_result[:3000]}"}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return schema(**json.loads(raw))
    except Exception as e:
        print(f"Extraction failed for {task_name}: {e}")
        return None

def run_structured_analysis() -> str:
    # Simulated tool results (raw, verbose)
    raw_results = {
        "Q1": """January revenue was $1,234,567 representing a 5.2% increase year-over-year.
                 February showed a decline to $1,134,200 (-8.1%). March recovered strongly at
                 $1,445,890 (+27.4%). Widget A was the top performer at $452,000 total.
                 Customer churn was measured at 3.2% for the quarter.""",
        "Q2": """Q2 showed consistent growth. April: $1,512,300 (+7.1%). May: $1,619,400 (+7.1%).
                 June softened to $1,312,100 (-18.9%). Widget B dominated at $521,000.
                 Churn improved to 2.8%.""",
    }

    # Extract structured metrics from each (raw: ~200 tokens → structured: ~80 tokens)
    all_metrics: list[SalesMetrics] = []
    for period, raw in raw_results.items():
        metrics = extract_structured(raw, SalesMetrics, period)
        if metrics:
            all_metrics.append(metrics)
            print(f"Extracted {period}: revenue=${metrics.total_revenue:,.0f}, growth={metrics.growth_pct}%")

    # Synthesise using compact structured data only
    metrics_json = json.dumps([m.model_dump() for m in all_metrics], indent=2)
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="Write a concise sales analysis from the provided structured metrics.",
        messages=[{"role": "user", "content": f"Quarterly metrics:\n{metrics_json}"}],
    )
    return response.content[0].text

print(run_structured_analysis())
```

**Expected Token Savings:** Raw API response: ~500-2,000 tokens. Structured extract: ~50-100 tokens. For 10 subtasks: 10,000-20,000 raw tokens → 500-1,000 structured tokens. 95%+ reduction. Also prevents hallucination in synthesis since the model works from validated structured data.
**Environment:** Requires defining schemas upfront. Best for predictable tool outputs (APIs, databases). For unstructured text (web search, documents), use Option 1 (narrative summary) instead.

---

### Option 4: Checkpoint-and-Resume with External State

Write completed subtask results to external storage (file, database) and inject only references into context. On resume, load only what's needed for the current step.

```python
import json
import hashlib
from pathlib import Path
import anthropic

CHECKPOINT_DIR = Path(".task_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)

def save_checkpoint(task_id: str, step: str, data: dict) -> str:
    """Save step result to disk, return a short reference."""
    key = hashlib.sha256(f"{task_id}:{step}".encode()).hexdigest()[:12]
    path = CHECKPOINT_DIR / f"{key}.json"
    path.write_text(json.dumps({"task_id": task_id, "step": step, "data": data}))
    return f"checkpoint://{key}"

def load_checkpoint(ref: str) -> dict:
    """Load checkpoint data from reference."""
    key = ref.replace("checkpoint://", "")
    path = CHECKPOINT_DIR / f"{key}.json"
    return json.loads(path.read_text())["data"]

def run_with_checkpoints(task_id: str, steps: list[str]) -> str:
    client = anthropic.Anthropic()

    # Only keep checkpoint references in context — not the data
    completed_refs: dict[str, str] = {}  # step → checkpoint_ref

    for step in steps:
        # Check if already done
        if step in completed_refs:
            print(f"Skipping {step} — already checkpointed")
            continue

        # Simulate expensive subtask
        raw_data = {
            "step": step,
            "records": list(range(1000)),  # would be huge in practice
            "summary": f"Processed {step}: found 42 items of interest",
        }

        # Save full data to disk, keep only reference in context
        ref = save_checkpoint(task_id, step, raw_data)
        completed_refs[step] = ref
        print(f"Checkpointed {step} → {ref}")

    # Build synthesis context from summaries only (not raw data)
    step_summaries = []
    for step, ref in completed_refs.items():
        data = load_checkpoint(ref)
        step_summaries.append(f"- {step}: {data['summary']}")

    context = "\n".join(step_summaries)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="Synthesise results from all completed steps.",
        messages=[{
            "role": "user",
            "content": f"Completed steps (full data in checkpoints):\n{context}\n\nWrite final report."
        }],
    )
    return response.content[0].text

result = run_with_checkpoints("analysis_001", [f"data_source_{i}" for i in range(10)])
print(result)
```

**Expected Token Savings:** Context contains only step references (~20 tokens each) instead of full results (~500-5,000 tokens each). For 10 steps: 200 tokens in context vs 50,000 tokens without checkpointing — 99.6% reduction. Also enables task resumability after crashes.
**Environment:** Requires file or database access. Checkpoint directory should be cleaned up after task completion. Works for multi-day long-running tasks where context would otherwise expire.

---

### Option 5: Map-Reduce Pattern — Summarise Each Chunk, Then Synthesise

For processing large collections, map each item to a summary (in parallel), then reduce summaries to a final output — never loading all raw data at once.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def map_item(item: str, item_id: str) -> str:
    """Extract key information from one item — the 'map' step."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system="Extract 2-3 key facts from this item. Be concise.",
        messages=[{"role": "user", "content": item}],
    )
    return f"[{item_id}] {response.content[0].text.strip()}"

async def reduce_summaries(summaries: list[str], original_task: str) -> str:
    """Synthesise all map outputs into a final result — the 'reduce' step."""
    combined = "\n".join(summaries)
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are completing this task: {original_task}\n\nSynthesize the provided summaries into a final answer.",
        messages=[{"role": "user", "content": f"Summaries:\n{combined}"}],
    )
    return response.content[0].text

async def map_reduce_agent(items: list[str], task: str, batch_size: int = 5) -> str:
    """Process items in parallel batches, then reduce."""
    all_summaries: list[str] = []

    # Map phase: process items in parallel batches
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        batch_summaries = await asyncio.gather(*[
            map_item(item, f"item_{i + j}")
            for j, item in enumerate(batch)
        ])
        all_summaries.extend(batch_summaries)
        print(f"Mapped batch {i//batch_size + 1}: {len(batch)} items → {sum(len(s) for s in batch_summaries)} chars")

    # Optional: hierarchical reduce for very large collections
    while len(all_summaries) > 20:
        mid_summaries = []
        for i in range(0, len(all_summaries), 10):
            chunk = all_summaries[i:i + 10]
            intermediate = await reduce_summaries(chunk, f"intermediate reduction for {task}")
            mid_summaries.append(intermediate)
        all_summaries = mid_summaries
        print(f"Hierarchical reduce: {len(all_summaries)} intermediate summaries")

    # Final reduce
    return await reduce_summaries(all_summaries, task)

# Example: analyse 30 customer reviews
reviews = [
    f"Review {i}: Product quality was {'excellent' if i % 3 == 0 else 'poor' if i % 3 == 1 else 'average'}. "
    f"Shipping took {i % 5 + 1} days. {'Would recommend.' if i % 2 == 0 else 'Would not recommend.'}"
    for i in range(30)
]

result = asyncio.run(map_reduce_agent(
    reviews,
    "Summarise overall customer sentiment and identify top issues",
    batch_size=5,
))
print(result)
```

**Expected Token Savings:** Map phase: 30 items × 100 tokens output = 3,000 tokens. Reduce input: 30 summaries × 30 tokens each = 900 tokens. Total: 3,900 tokens vs loading all 30 reviews (30 × 500 = 15,000 tokens) into a single context. 74% reduction. Hierarchical reduce handles unlimited scale.
**Environment:** Parallel map requires async client. Batch size should be tuned to API concurrency limits. Hierarchical reduce adds latency but handles collections of any size.

---

### Option 6: Sliding Window Context — Keep Only the Most Recent N Turns Raw

Maintain a sliding window of the last N complete turns in raw form; compress everything older into a rolling summary prepended to the window.

```python
import anthropic
from collections import deque

client = anthropic.Anthropic()

class SlidingWindowContext:
    def __init__(self, raw_window: int = 6, summary_max_tokens: int = 400):
        self.raw_window = raw_window
        self.summary_max_tokens = summary_max_tokens
        self._window: deque[dict] = deque(maxlen=raw_window)
        self._rolling_summary: str = ""

    def add(self, message: dict) -> None:
        if len(self._window) == self.raw_window:
            # Window full: compress the oldest message into summary
            oldest = self._window[0]
            self._extend_summary(oldest)
        self._window.append(message)

    def _extend_summary(self, message: dict) -> None:
        content = str(message.get("content", ""))[:1000]
        role = message.get("role", "")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system="Update the running summary with the new message. Be concise; preserve key facts.",
            messages=[{
                "role": "user",
                "content": (
                    f"Current summary:\n{self._rolling_summary or '(none yet)'}\n\n"
                    f"New message ({role}):\n{content}\n\n"
                    "Updated summary:"
                ),
            }],
        )
        self._rolling_summary = response.content[0].text.strip()

    def build_messages(self) -> list[dict]:
        """Build message list: summary injection + raw window."""
        messages = []
        if self._rolling_summary:
            messages.append({
                "role": "user",
                "content": f"[Earlier context summary]\n{self._rolling_summary}",
            })
            messages.append({
                "role": "assistant",
                "content": "I have the context from earlier.",
            })
        messages.extend(list(self._window))
        return messages

def run_with_sliding_window(subtasks: list[tuple[str, str]]) -> str:
    ctx = SlidingWindowContext(raw_window=4)

    for user_msg, tool_result in subtasks:
        ctx.add({"role": "user", "content": user_msg})
        ctx.add({"role": "assistant", "content": f"Processing: {tool_result[:100]}..."})
        ctx.add({"role": "user", "content": f"[Tool result]\n{tool_result}"})

        messages = ctx.build_messages()
        print(f"Context size: {len(messages)} messages, summary: {len(ctx._rolling_summary)} chars")

    # Final step
    ctx.add({"role": "user", "content": "Provide final consolidated report."})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=ctx.build_messages(),
    )
    return response.content[0].text

# Comparison table
"""
| Approach | Compression Type | Latency | Info Loss Risk | Best For |
|---|---|---|---|---|
| Option 1: Post-step summarise | Narrative summary | +1 call/step | Low-Medium | File/API results |
| Option 2: Rolling compaction | Bulk conversation compress | +1 call/threshold | Medium | Long conversations |
| Option 3: Structured extract | Schema-validated extract | +1 call/step | Low | Typed API responses |
| Option 4: Checkpoint+reference | External storage | I/O overhead | Very Low | Resumable tasks |
| Option 5: Map-reduce | Hierarchical summarise | Parallel latency | Low-Medium | Large collections |
| Option 6: Sliding window | Rolling oldest-message compress | +1 call/eviction | Medium | Multi-turn chat |
"""

subtasks = [
    (f"Process data source {i}", "record_id,value,status\n" + "\n".join(f"{j},{j*10},active" for j in range(50)))
    for i in range(8)
]
result = run_with_sliding_window(subtasks)
print(result)
```

**Expected Token Savings:** Raw window = last 4 messages = ~2,000 tokens max. Rolling summary = ~400 tokens. Total context: ~2,400 tokens regardless of task length. Without sliding window, 8-step task: ~16,000 tokens. Savings: 85% for this example; scales to 99%+ for 50+ step tasks.
**Environment:** Window size should cover enough context for coherent reasoning (typically 3-6 turns). Summary model should use `claude-haiku-4-5-20251001` to keep compaction cost low. Combine with Option 4 (checkpoints) for full data recovery if needed.
