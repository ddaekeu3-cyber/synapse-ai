---
layout: solution
title: "Agent Doesn't Implement Context Window Packing for Batch Processing"
category: token-cost
description: "Naively sending one item per API call wastes expensive context window capacity. Packing multiple small items into a single call — with clear delimiters and structured output — can reduce API call count and total cost by 5-20x for batch workloads."
tags: [token-cost, batch-processing, context-packing, efficiency, throughput]
---

## Problem

Agents that process lists of items (documents, records, strings) often send one item per API call. Each call carries overhead: system prompt, formatting, and output structure repeated N times. Context window packing fits multiple items into one call, amortizing this overhead across many items — dramatically reducing both API call count and total token cost for batch workloads.

## Solutions

### Option 1: Simple Fixed-Batch Packing

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class BatchResult:
    item_id: str
    output: str

def process_single(item: str, task: str) -> str:
    """Naive one-item-per-call baseline."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"{task}: {item}"}]
    )
    return response.content[0].text.strip()

def process_batch_packed(items: list[str], task: str, batch_size: int = 10) -> list[BatchResult]:
    """
    Pack multiple items into one call.
    Each item is numbered; model returns numbered outputs.
    """
    results: list[BatchResult] = []

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]

        # Build numbered list prompt
        items_text = "\n".join(f"{j+1}. {item}" for j, item in enumerate(batch))
        prompt = f"""{task} for each of the following items.
Return ONLY a numbered list matching the input order, one result per line.

Items:
{items_text}

Results (numbered 1-{len(batch)}):"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50 * len(batch),  # Scale with batch size
            messages=[{"role": "user", "content": prompt}]
        )
        output_text = response.content[0].text.strip()

        # Parse numbered outputs
        import re
        lines = output_text.split("\n")
        parsed_outputs: dict[int, str] = {}
        for line in lines:
            match = re.match(r'^(\d+)[.)]\s*(.+)$', line.strip())
            if match:
                idx = int(match.group(1)) - 1
                parsed_outputs[idx] = match.group(2).strip()

        for j, item in enumerate(batch):
            item_id = f"item_{i+j}"
            output = parsed_outputs.get(j, f"[parse error for item {j+1}]")
            results.append(BatchResult(item_id=item_id, output=output))

    return results

# Benchmark comparison
import time

product_names = [
    "UltraBoost Running Shoes",
    "CloudComfort Memory Foam Pillow",
    "TechPro Wireless Headphones",
    "GreenLeaf Bamboo Water Bottle",
    "SwiftCharge USB-C Hub",
    "CozyCashmere Winter Scarf",
    "ProGrip Kitchen Knife Set",
    "AquaPure Water Filter"
]

task = "Write a 5-word marketing tagline"

# Naive approach timing
t0 = time.time()
single_results = [process_single(p, task) for p in product_names]
single_time = time.time() - t0
print(f"Single-call: {len(product_names)} calls, {single_time:.1f}s")

# Packed approach timing
t0 = time.time()
packed_results = process_batch_packed(product_names, task, batch_size=8)
packed_time = time.time() - t0
print(f"Packed: 1 call, {packed_time:.1f}s ({single_time/max(packed_time,0.1):.1f}x faster)")

for r in packed_results[:4]:
    print(f"  {r.item_id}: {r.output}")

# Expected Token Savings: 60-80% reduction in overhead tokens; 8x fewer API calls
# Environment: ANTHROPIC_API_KEY required
```

### Option 2: Token-Aware Dynamic Packing

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Iterator

client = anthropic.Anthropic()

# Approximate tokens: 1 token ≈ 4 chars
def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1

@dataclass
class PackedBatch:
    items: list[tuple[str, str]]   # [(item_id, item_content)]
    estimated_input_tokens: int
    estimated_output_tokens: int

    @property
    def total_estimated_tokens(self) -> int:
        return self.estimated_input_tokens + self.estimated_output_tokens

def pack_items_by_token_budget(
    items: list[tuple[str, str]],   # [(id, content)]
    task_description: str,
    system_prompt: str = "",
    max_input_tokens: int = 3000,
    per_item_output_tokens: int = 60,
    overhead_tokens: int = 200      # prompt structure overhead
) -> Iterator[PackedBatch]:
    """
    Yield batches that fit within token budget.
    Dynamically sizes each batch based on item lengths.
    """
    current_batch: list[tuple[str, str]] = []
    current_input_tokens = overhead_tokens + estimate_tokens(task_description) + estimate_tokens(system_prompt)

    for item_id, content in items:
        item_tokens = estimate_tokens(content) + 10  # +10 for numbering/formatting

        if current_batch and (current_input_tokens + item_tokens > max_input_tokens):
            # Yield current batch and start new one
            output_budget = per_item_output_tokens * len(current_batch)
            yield PackedBatch(
                items=current_batch,
                estimated_input_tokens=current_input_tokens,
                estimated_output_tokens=output_budget
            )
            current_batch = []
            current_input_tokens = overhead_tokens + estimate_tokens(task_description)

        current_batch.append((item_id, content))
        current_input_tokens += item_tokens

    if current_batch:
        yield PackedBatch(
            items=current_batch,
            estimated_input_tokens=current_input_tokens,
            estimated_output_tokens=per_item_output_tokens * len(current_batch)
        )

def execute_packed_batch(
    batch: PackedBatch,
    task: str,
    system: str = ""
) -> dict[str, str]:
    """Execute one packed batch, return {item_id: output}."""
    items_text = "\n".join(
        f"{i+1}. [{item_id}] {content}"
        for i, (item_id, content) in enumerate(batch.items)
    )

    prompt = f"""{task}

Process each item below. For each, respond on ONE line in format:
<item_id>: <your response>

Items:
{items_text}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=batch.estimated_output_tokens,
        system=system or "Process items accurately and concisely.",
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    # Parse <item_id>: <response> lines
    results: dict[str, str] = {}
    for line in output.strip().split("\n"):
        match = re.match(r'^(\S+):\s*(.+)$', line.strip())
        if match:
            results[match.group(1)] = match.group(2).strip()

    return results

def batch_process(
    items: dict[str, str],  # {item_id: content}
    task: str,
    system: str = "",
    max_input_tokens: int = 3000
) -> dict[str, str]:
    """Process all items with dynamic token-aware packing."""
    all_results: dict[str, str] = {}
    item_list = list(items.items())

    batches = list(pack_items_by_token_budget(item_list, task, system, max_input_tokens))
    print(f"[Packing] {len(item_list)} items → {len(batches)} batches "
          f"(avg {len(item_list)//max(len(batches),1)} items/batch)")

    for i, batch in enumerate(batches):
        print(f"  Batch {i+1}: {len(batch.items)} items, ~{batch.estimated_input_tokens} input tokens")
        results = execute_packed_batch(batch, task, system)
        all_results.update(results)

    return all_results

# Usage
documents = {
    "doc1": "Python is a high-level programming language known for readability.",
    "doc2": "Machine learning is a subset of artificial intelligence.",
    "doc3": "REST APIs use HTTP methods to exchange data between systems.",
    "doc4": "Docker containers package applications with their dependencies.",
    "doc5": "Git is a distributed version control system created by Linus Torvalds.",
    "doc6": "Kubernetes orchestrates containerized applications across clusters.",
    "doc7": "SQL is used to query and manipulate relational databases.",
    "doc8": "JSON is a lightweight data interchange format based on JavaScript.",
    "doc9": "Async programming allows non-blocking I/O operations in Python.",
    "doc10": "Type hints in Python improve code readability and enable static analysis.",
}

results = batch_process(
    documents,
    task="Generate a 6-word summary",
    system="You are a technical documentation summarizer.",
    max_input_tokens=2000
)

for item_id, summary in list(results.items())[:5]:
    print(f"  {item_id}: {summary}")

# Expected Token Savings: 65-75% overhead reduction; dynamic sizing handles variable-length items
# Environment: ANTHROPIC_API_KEY required
```

### Option 3: Async Parallel Packed Batches

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass
from math import ceil

client = anthropic.AsyncAnthropic()

@dataclass
class AsyncBatchResult:
    batch_index: int
    item_count: int
    results: dict[str, str]
    input_tokens: int
    output_tokens: int

async def process_async_batch(
    batch_index: int,
    items: list[tuple[str, str]],
    task: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001"
) -> AsyncBatchResult:
    """Process one batch asynchronously."""
    items_text = "\n".join(
        f"{i+1}. {content}"
        for i, (_, content) in enumerate(items)
    )

    prompt = f"""{task}

Return ONLY a numbered list (1 to {len(items)}), one per line:
{items_text}"""

    response = await client.messages.create(
        model=model,
        max_tokens=80 * len(items),
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    results: dict[str, str] = {}
    for line in output.strip().split("\n"):
        match = re.match(r'^(\d+)[.)]\s*(.+)$', line.strip())
        if match:
            idx = int(match.group(1)) - 1
            if 0 <= idx < len(items):
                item_id = items[idx][0]
                results[item_id] = match.group(2).strip()

    return AsyncBatchResult(
        batch_index=batch_index,
        item_count=len(items),
        results=results,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens
    )

async def async_packed_batch_process(
    items: dict[str, str],
    task: str,
    system: str = "",
    batch_size: int = 10,
    max_concurrent: int = 5,
    model: str = "claude-haiku-4-5-20251001"
) -> dict[str, str]:
    """
    Split items into batches, process all batches in parallel.
    Combines packing efficiency + async parallelism.
    """
    item_list = list(items.items())
    batches = [
        item_list[i:i+batch_size]
        for i in range(0, len(item_list), batch_size)
    ]

    print(f"[AsyncPack] {len(item_list)} items → {len(batches)} batches of ≤{batch_size}, "
          f"max {max_concurrent} concurrent")

    semaphore = asyncio.Semaphore(max_concurrent)

    async def bounded_batch(idx: int, batch: list) -> AsyncBatchResult:
        async with semaphore:
            return await process_async_batch(idx, batch, task, system, model)

    batch_results = await asyncio.gather(*[
        bounded_batch(i, batch) for i, batch in enumerate(batches)
    ])

    all_results: dict[str, str] = {}
    total_in = total_out = 0
    for br in batch_results:
        all_results.update(br.results)
        total_in += br.input_tokens
        total_out += br.output_tokens

    print(f"[AsyncPack] Done: {total_in} input + {total_out} output tokens total")
    return all_results

async def main():
    import time

    # 40 items to classify
    reviews = {
        f"review_{i}": text for i, text in enumerate([
            "Absolutely loved it! Will buy again.",
            "Product broke after 2 days. Very disappointed.",
            "Average quality, nothing special.",
            "Fast shipping, great packaging!",
            "Does not match description at all.",
            "Exceeded my expectations in every way.",
            "Decent product for the price.",
            "Terrible customer service experience.",
            "Works perfectly, highly recommend.",
            "Returned immediately, poor quality.",
        ] * 4)  # 40 reviews
    }

    t0 = time.time()
    results = await async_packed_batch_process(
        reviews,
        task="Classify sentiment as POSITIVE, NEGATIVE, or NEUTRAL",
        batch_size=10,
        max_concurrent=4
    )
    elapsed = time.time() - t0

    counts = {"POSITIVE": 0, "NEGATIVE": 0, "NEUTRAL": 0}
    for v in results.values():
        for sentiment in counts:
            if sentiment in v.upper():
                counts[sentiment] += 1
                break

    print(f"\nClassified {len(results)}/40 reviews in {elapsed:.1f}s")
    print(f"Sentiments: {counts}")

asyncio.run(main())

# Expected Token Savings: 70% overhead reduction + 4x speed from parallelism
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 4: Multi-Task Packing (Heterogeneous Operations)

```python
import anthropic
import json
import re
from dataclasses import dataclass
from typing import Any

client = anthropic.Anthropic()

@dataclass
class Task:
    task_id: str
    task_type: str   # "summarize" | "classify" | "extract" | "translate"
    content: str
    extra_params: dict = None

@dataclass
class TaskResult:
    task_id: str
    task_type: str
    output: Any
    success: bool

def pack_heterogeneous_tasks(tasks: list[Task]) -> str:
    """
    Pack different task types into one prompt using structured XML.
    Each task is self-describing.
    """
    task_blocks = []
    for t in tasks:
        params = f' params="{json.dumps(t.extra_params or {})}"' if t.extra_params else ""
        task_blocks.append(
            f'<task id="{t.task_id}" type="{t.task_type}"{params}>\n{t.content}\n</task>'
        )
    return "\n\n".join(task_blocks)

def parse_heterogeneous_results(output: str, tasks: list[Task]) -> list[TaskResult]:
    """Parse structured results back to individual task outputs."""
    results = []
    for task in tasks:
        # Look for <result id="task_id">...</result>
        pattern = rf'<result\s+id="{re.escape(task.task_id)}"[^>]*>(.*?)</result>'
        match = re.search(pattern, output, re.DOTALL | re.IGNORECASE)
        if match:
            content = match.group(1).strip()
            results.append(TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                output=content,
                success=True
            ))
        else:
            results.append(TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                output=None,
                success=False
            ))
    return results

def process_heterogeneous_batch(tasks: list[Task]) -> list[TaskResult]:
    """Send diverse tasks in one API call using structured prompting."""
    packed_tasks = pack_heterogeneous_tasks(tasks)

    system = """You are a multi-task processor. Process each <task> element independently.
For each task, respond with <result id="<task_id>" type="<task_type>">your output</result>.
Task types:
- summarize: Write a 1-sentence summary
- classify: Classify into the requested categories
- extract: Extract the requested information as JSON
- translate: Translate to the requested language"""

    prompt = f"""Process all tasks below and return structured results.

{packed_tasks}

Respond with one <result> block per task, in order."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100 * len(tasks),
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )

    print(f"[HeteroPack] {len(tasks)} tasks → 1 call | {response.usage.input_tokens}in/{response.usage.output_tokens}out tokens")
    return parse_heterogeneous_results(response.content[0].text, tasks)

# Usage: diverse tasks in one call
tasks = [
    Task("t1", "summarize", "Python's asyncio library provides infrastructure for writing single-threaded concurrent code using coroutines, multiplexing I/O access over sockets and other resources, running network clients and servers, and other related primitives."),
    Task("t2", "classify", "This product is amazing! The battery lasts all day and the screen is crystal clear. Best phone I've ever owned.", {"categories": ["positive", "negative", "neutral"]}),
    Task("t3", "extract", 'Contact: John Smith, email: john@example.com, phone: 555-1234, role: Senior Engineer', {"fields": ["name", "email", "phone", "role"]}),
    Task("t4", "summarize", "Docker is a platform for developing, shipping, and running applications in containers. Containers allow developers to package an application with all its dependencies into a standardized unit."),
    Task("t5", "classify", "The shipping took 3 weeks and the item arrived damaged. Very frustrating experience.", {"categories": ["positive", "negative", "neutral"]}),
    Task("t6", "extract", "Meeting on 2024-03-15 at 2pm PST. Attendees: Alice, Bob, Carol. Topic: Q2 roadmap planning.", {"fields": ["date", "time", "attendees", "topic"]}),
]

results = process_heterogeneous_batch(tasks)
for r in results:
    status = "✓" if r.success else "✗"
    print(f"  [{status}] {r.task_id} ({r.task_type}): {str(r.output)[:80]}")

# Expected Token Savings: 50-70% overhead reduction for heterogeneous workloads
# Environment: ANTHROPIC_API_KEY required
```

### Option 5: Streaming Packed Batch with Progressive Output

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

client = anthropic.Anthropic()

@dataclass
class StreamingPackResult:
    item_id: str
    content: str
    complete: bool = False

def process_streaming_batch(
    items: list[tuple[str, str]],   # [(item_id, content)]
    task: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    on_item_complete: Optional[Callable[[StreamingPackResult], None]] = None
) -> dict[str, str]:
    """
    Pack items into one call and stream results.
    Fires callback as each item's result completes in the stream.
    """
    items_text = "\n".join(
        f'<item id="{item_id}">{content}</item>'
        for item_id, content in items
    )

    prompt = f"""{task}

Process each item and output results as:
<result id="ITEM_ID">your output</result>

Items:
{items_text}"""

    buffer = ""
    completed_results: dict[str, str] = {}
    result_pattern = re.compile(r'<result\s+id="([^"]+)">(.*?)</result>', re.DOTALL)

    with client.messages.stream(
        model=model,
        max_tokens=80 * len(items),
        system=system or "Process each item accurately.",
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for chunk in stream.text_stream:
            buffer += chunk

            # Check for completed result tags in buffer
            for match in result_pattern.finditer(buffer):
                item_id = match.group(1)
                content = match.group(2).strip()

                if item_id not in completed_results:
                    completed_results[item_id] = content
                    result = StreamingPackResult(item_id=item_id, content=content, complete=True)

                    if on_item_complete:
                        on_item_complete(result)

            # Trim processed content from buffer (keep potential partial match)
            last_end = max((m.end() for m in result_pattern.finditer(buffer)), default=0)
            if last_end > 0:
                buffer = buffer[last_end:]

    return completed_results

# Usage with progressive callback
completed_count = [0]

def on_result(result: StreamingPackResult):
    completed_count[0] += 1
    print(f"  [{completed_count[0]}] {result.item_id}: {result.content[:60]}")

items_to_process = [
    ("prod_1", "UltraMax 4K Monitor - 32 inch IPS display with USB-C hub"),
    ("prod_2", "ErgoDesk Standing Desk Converter - adjustable height"),
    ("prod_3", "MechKey Pro Mechanical Keyboard - tactile switches"),
    ("prod_4", "WebCam HD Pro - 1080p with ring light"),
    ("prod_5", "USB Hub 7-Port - with individual power switches"),
]

print("[Streaming Pack] Processing 5 items...")
results = process_streaming_batch(
    items_to_process,
    task="Write a 5-word product tagline",
    on_item_complete=on_result
)

print(f"\nFinal results ({len(results)}/{len(items_to_process)} parsed):")
for item_id, tagline in results.items():
    print(f"  {item_id}: {tagline}")

# Expected Token Savings: Packing + streaming enables progressive display while saving overhead tokens
# Environment: ANTHROPIC_API_KEY required, uses streaming API
```

### Option 6: Adaptive Batch Sizing Based on Context Utilization

```python
import anthropic
import re
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class BatchStats:
    batch_size: int
    input_tokens: int
    output_tokens: int
    duration_ms: float
    items_processed: int
    parse_success_rate: float

    @property
    def tokens_per_item(self) -> float:
        return (self.input_tokens + self.output_tokens) / max(self.items_processed, 1)

    @property
    def items_per_second(self) -> float:
        return self.items_processed / max(self.duration_ms / 1000, 0.001)

class AdaptiveBatchProcessor:
    """
    Learns optimal batch size by monitoring token utilization and parse success rate.
    Grows batch size when successful, shrinks on parse failures.
    """
    def __init__(self, initial_batch_size: int = 5, model: str = "claude-haiku-4-5-20251001"):
        self.model = model
        self.batch_size = initial_batch_size
        self.min_batch = 2
        self.max_batch = 25
        self.history: list[BatchStats] = []
        self._consecutive_successes = 0
        self._consecutive_failures = 0

    def _adjust_batch_size(self, success_rate: float):
        if success_rate >= 0.95:
            self._consecutive_successes += 1
            self._consecutive_failures = 0
            if self._consecutive_successes >= 3:
                self.batch_size = min(self.batch_size + 2, self.max_batch)
                self._consecutive_successes = 0
                print(f"  [Adaptive] Growing batch → {self.batch_size}")
        elif success_rate < 0.8:
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            if self._consecutive_failures >= 2:
                self.batch_size = max(self.batch_size - 2, self.min_batch)
                self._consecutive_failures = 0
                print(f"  [Adaptive] Shrinking batch → {self.batch_size}")

    def process_batch(self, items: list[tuple[str, str]], task: str) -> dict[str, str]:
        items_text = "\n".join(f"{i+1}. [{iid}] {content}" for i, (iid, content) in enumerate(items))
        prompt = f"""{task}

Return ONLY numbered results matching input order:
{items_text}

Format: <N>. [ITEM_ID] result"""

        t0 = time.time()
        response = client.messages.create(
            model=self.model,
            max_tokens=80 * len(items),
            messages=[{"role": "user", "content": prompt}]
        )
        duration = (time.time() - t0) * 1000
        output = response.content[0].text

        results: dict[str, str] = {}
        pattern = re.compile(r'\d+\.\s*\[([^\]]+)\]\s*(.+?)(?=\n\d+\.|$)', re.DOTALL)
        for match in pattern.finditer(output):
            results[match.group(1)] = match.group(2).strip()

        success_rate = len(results) / len(items)
        stats = BatchStats(
            batch_size=len(items),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=duration,
            items_processed=len(results),
            parse_success_rate=success_rate
        )
        self.history.append(stats)
        self._adjust_batch_size(success_rate)

        print(f"  Batch={len(items)}, parsed={len(results)}/{len(items)} ({success_rate:.0%}), "
              f"{stats.tokens_per_item:.0f} tok/item, {stats.items_per_second:.1f} items/s")

        return results

    def process_all(self, items: dict[str, str], task: str) -> dict[str, str]:
        item_list = list(items.items())
        all_results: dict[str, str] = {}
        i = 0
        batch_num = 0

        print(f"[Adaptive] Starting with batch_size={self.batch_size}")
        while i < len(item_list):
            batch = item_list[i:i + self.batch_size]
            batch_num += 1
            print(f"\nBatch {batch_num} (items {i+1}-{i+len(batch)}):")
            results = self.process_batch(batch, task)
            all_results.update(results)
            i += self.batch_size

        if self.history:
            avg_tpi = sum(s.tokens_per_item for s in self.history) / len(self.history)
            print(f"\n[Adaptive] Summary: {len(self.history)} batches, avg {avg_tpi:.0f} tokens/item")

        return all_results

# Usage
processor = AdaptiveBatchProcessor(initial_batch_size=4)

sentences = {
    f"s{i}": text for i, text in enumerate([
        "The quick brown fox jumps over the lazy dog.",
        "Machine learning models require large amounts of training data.",
        "Kubernetes simplifies container orchestration at scale.",
        "Python's GIL prevents true parallelism for CPU-bound tasks.",
        "Microservices improve scalability but add operational complexity.",
        "GraphQL allows clients to request exactly the data they need.",
        "Redis is an in-memory data store used for caching and messaging.",
        "Terraform enables infrastructure as code across cloud providers.",
        "WebAssembly allows high-performance code to run in browsers.",
        "Event-driven architecture decouples services through message queues.",
        "CI/CD pipelines automate testing and deployment workflows.",
        "Serverless computing abstracts infrastructure management entirely.",
    ])
}

results = processor.process_all(sentences, "Classify as: technical, general, or business")
print(f"\nClassified {len(results)}/{len(sentences)} items")

# Expected Token Savings: Adaptive sizing maximizes context utilization; learns optimal batch from live metrics
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Batch Strategy | Handles Mixed Lengths | Async | Parse Method | Best Use Case |
|--------|---------------|----------------------|-------|-------------|---------------|
| Simple Fixed Batch | Fixed N items/call | Poorly | No | Numbered list | Uniform short items |
| Token-Aware Dynamic | Token budget packing | Yes | No | ID-tagged lines | Variable-length items |
| Async Parallel Batches | Fixed + concurrent | Poorly | Yes | Numbered list | High-throughput pipelines |
| Heterogeneous Multi-Task | Task-type mixing | Yes | No | XML result tags | Diverse operation types |
| Streaming Packed Batch | Fixed + streaming | Poorly | No | XML streaming | Progressive display UX |
| Adaptive Sizing | Self-tuning | Yes | No | ID-tagged + regex | Unknown item distributions |
