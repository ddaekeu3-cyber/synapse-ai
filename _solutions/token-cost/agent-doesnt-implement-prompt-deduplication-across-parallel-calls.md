---
layout: solution
title: "Agent Doesn't Implement Prompt Deduplication Across Parallel Calls"
category: token-cost
description: "When running multiple LLM calls that share common context, deduplicate the shared portions using prompt caching — avoiding redundant token charges across parallel requests."
tags: [token-cost, prompt-caching, deduplication, parallel, cost-optimization, cache_control]
---

## Problem

Parallel LLM calls frequently share large blocks of identical content: the same system prompt, the same document corpus, the same tool definitions. Each call charges full input tokens for this shared content. A batch of 20 parallel analysis calls sharing a 10,000-token document each pays 200,000 tokens for the document alone — instead of paying once and caching.

```python
# Naive: every parallel call re-sends the full document
async def analyze_all(doc: str, questions: list[str]) -> list[str]:
    tasks = [
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"{doc}\n\nQ: {q}"}],
        )
        for q in questions
    ]
    # Each call sends full doc — O(n × doc_tokens) instead of O(doc_tokens + n × question_tokens)
    return [r.content[0].text for r in await asyncio.gather(*tasks)]
```

## Solution Options

### Option 1: Shared System Prompt Caching for Parallel Calls

Move shared context into the system prompt with `cache_control`. All parallel user-turn calls share the cached system prompt — charged only on the first call.

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def analyze_document_parallel(
    document: str,
    questions: list[str],
    model: str = "claude-haiku-4-5-20251001",
) -> list[str]:
    # System prompt carries the shared document with cache_control
    system = [
        {
            "type": "text",
            "text": f"You are a document analysis assistant.\n\nDocument to analyze:\n{document}",
            "cache_control": {"type": "ephemeral"},
        }
    ]

    async def ask_one(question: str) -> str:
        r = await client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        usage = r.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_created = getattr(usage, "cache_creation_input_tokens", 0)
        print(f"Q: {question[:40]!r} | cache_read={cache_read} cache_created={cache_created}")
        return r.content[0].text

    # Fire all questions in parallel — only first call pays for cache creation
    results = await asyncio.gather(*[ask_one(q) for q in questions])
    return results


async def main():
    document = (
        "The Python programming language was created by Guido van Rossum and first released in 1991. "
        "Python emphasizes code readability and simplicity. It supports multiple programming paradigms "
        "including procedural, object-oriented, and functional programming. "
        "Python's standard library is extensive, covering areas from file I/O to networking. "
        "Version 3.0 was released in 2008 and introduced several backward-incompatible changes. "
    ) * 20  # ~2000 tokens shared across all calls

    questions = [
        "When was Python first released?",
        "Who created Python?",
        "What programming paradigms does Python support?",
        "What changed in Python 3.0?",
        "Describe Python's philosophy.",
    ]

    answers = await analyze_document_parallel(document, questions)
    for q, a in zip(questions, answers):
        print(f"Q: {q}\nA: {a[:100]}\n")

asyncio.run(main())

# Expected Token Savings: ~80% reduction on shared document tokens after first call; cache_read ~10% cost of input
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Prefix-Shared Message Structure for Batch Analysis

Structure all parallel calls so the shared prefix (document + instructions) appears as identical leading message blocks with `cache_control`. The unique question is the final message.

```python
import anthropic
import asyncio
from dataclasses import dataclass

@dataclass
class BatchAnalysisResult:
    question: str
    answer: str
    cache_read_tokens: int
    cache_created_tokens: int
    input_tokens: int

client = anthropic.AsyncAnthropic()

def build_shared_prefix(
    documents: list[str],
    shared_instructions: str,
) -> list[dict]:
    """Build the shared message prefix that will be cached across all parallel calls."""
    doc_block = "\n\n---\n\n".join(
        f"Document {i+1}:\n{doc}" for i, doc in enumerate(documents)
    )
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"{shared_instructions}\n\n{doc_block}",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {
            "role": "assistant",
            "content": "I have read all the documents and I'm ready to answer your questions.",
        },
    ]

async def batch_analyze(
    documents: list[str],
    questions: list[str],
    shared_instructions: str = "Analyze the following documents and answer questions precisely.",
    model: str = "claude-haiku-4-5-20251001",
) -> list[BatchAnalysisResult]:
    shared_prefix = build_shared_prefix(documents, shared_instructions)

    async def ask(question: str) -> BatchAnalysisResult:
        messages = shared_prefix + [{"role": "user", "content": question}]
        r = await client.messages.create(
            model=model,
            max_tokens=256,
            messages=messages,
        )
        u = r.usage
        return BatchAnalysisResult(
            question=question,
            answer=r.content[0].text,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0),
            cache_created_tokens=getattr(u, "cache_creation_input_tokens", 0),
            input_tokens=u.input_tokens,
        )

    results = await asyncio.gather(*[ask(q) for q in questions])

    total_saved = sum(r.cache_read_tokens for r in results)
    total_created = sum(r.cache_created_tokens for r in results)
    print(f"\n[CACHE STATS] created={total_created} read={total_saved} "
          f"savings≈{total_saved * 0.9:.0f} tokens (90% discount on reads)")
    return results


async def main():
    docs = [
        "Revenue Q1: $1.2M. Revenue Q2: $1.5M. Revenue Q3: $1.8M.",
        "Expenses Q1: $900K. Expenses Q2: $1.1M. Expenses Q3: $1.3M.",
        "Headcount grew from 45 to 67 employees between Q1 and Q3.",
    ]
    questions = [
        "What was total revenue for Q1-Q3?",
        "Calculate total expenses for all three quarters.",
        "What was the net profit margin in Q2?",
        "How much did headcount grow as a percentage?",
        "Which quarter had the best revenue growth?",
    ]
    results = await batch_analyze(docs, questions)
    for r in results:
        print(f"Q: {r.question}\nA: {r.answer[:120]}\n  cache_read={r.cache_read_tokens}\n")

asyncio.run(main())

# Expected Token Savings: 85-90% reduction on shared document tokens for calls 2+; ideal for FAQ-style batch analysis
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Tool Definition Deduplication for Multi-Tool Agents

When running parallel agent calls that all use the same tool set, cache the tool definitions to avoid re-charging for large tool schemas on every call.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

# Large tool definitions shared across all parallel calls
SHARED_TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the internal knowledge base for relevant information. "
                       "Supports semantic search, keyword search, and filtered queries. "
                       "Returns ranked results with relevance scores and source citations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {"type": "integer", "description": "Maximum results to return (1-20)"},
                "filter_category": {"type": "string", "description": "Optional category filter"},
                "include_metadata": {"type": "boolean", "description": "Include source metadata in results"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "execute_calculation",
        "description": "Execute mathematical calculations, statistical analysis, and data transformations. "
                       "Supports arithmetic, algebra, statistics (mean, median, std, percentiles), "
                       "financial formulas (NPV, IRR, compound interest), and unit conversions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "The mathematical expression or formula"},
                "variables": {"type": "object", "description": "Variable substitutions"},
                "output_format": {"type": "string", "enum": ["decimal", "percentage", "currency"]},
            },
            "required": ["expression"],
        },
    },
    {
        "name": "generate_report",
        "description": "Generate formatted reports in various output formats. Supports Markdown, HTML, JSON, "
                       "and PDF (via LaTeX). Can include charts, tables, executive summaries, and appendices.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "string"}},
                "format": {"type": "string", "enum": ["markdown", "html", "json"]},
                "include_summary": {"type": "boolean"},
            },
            "required": ["title", "sections"],
        },
    },
]

def _tools_with_cache() -> list[dict]:
    """Add cache_control to the last tool definition to cache entire tool block."""
    tools = [t.copy() for t in SHARED_TOOLS]
    tools[-1] = dict(tools[-1], cache_control={"type": "ephemeral"})
    return tools

@dataclass
class AgentResult:
    task: str
    response: str
    cache_read: int

async def parallel_agent_calls(tasks: list[str], system: str) -> list[AgentResult]:
    cached_tools = _tools_with_cache()

    async def run_task(task: str) -> AgentResult:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=cached_tools,
            messages=[{"role": "user", "content": task}],
        )
        cache_read = getattr(r.usage, "cache_read_input_tokens", 0)
        text = next(
            (b.text for b in r.content if hasattr(b, "text")),
            f"[tool_use: {r.content[0].name if r.content else 'none'}]",
        )
        return AgentResult(task=task[:50], response=text[:120], cache_read=cache_read)

    results = await asyncio.gather(*[run_task(t) for t in tasks])
    total_cache_read = sum(r.cache_read for r in results)
    print(f"[TOOL CACHE] Total cache_read_tokens={total_cache_read} across {len(tasks)} calls")
    return results


async def main():
    tasks = [
        "Search for information about Q3 revenue performance.",
        "Calculate the year-over-year growth rate if Q3 last year was $1.2M and this year is $1.8M.",
        "Generate a brief summary report of our Q3 metrics.",
        "Search for competitor pricing information.",
        "Calculate our profit margin given revenue $1.8M and costs $1.3M.",
    ]
    results = await parallel_agent_calls(tasks, "You are a business analysis assistant.")
    for r in results:
        print(f"Task: {r.task}\nResult: {r.response}\ncache_read={r.cache_read}\n")

asyncio.run(main())

# Expected Token Savings: Tool schemas often 500-2000 tokens; caching saves ~90% on reads for all parallel calls
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Deduplicated Batch Processor with Per-Request Unique Suffix

For workflows where many requests share a large common prompt, structure each request as `shared_prefix + unique_suffix`. The shared prefix is cached; only unique suffixes are charged at full price.

```python
import anthropic
import asyncio
import hashlib
from dataclasses import dataclass

@dataclass
class DeduplicatedRequest:
    shared_prefix: str
    unique_suffix: str
    request_id: str

@dataclass
class DeduplicatedResult:
    request_id: str
    response: str
    shared_tokens_read_from_cache: int
    unique_tokens_billed: int

client = anthropic.AsyncAnthropic()

class DeduplicatedBatchProcessor:
    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.model = model
        self._prefix_hash_to_cache: dict[str, bool] = {}

    def _hash_prefix(self, prefix: str) -> str:
        return hashlib.md5(prefix.encode()).hexdigest()[:8]

    async def process_batch(
        self,
        requests: list[DeduplicatedRequest],
        max_concurrency: int = 5,
    ) -> list[DeduplicatedResult]:
        sem = asyncio.Semaphore(max_concurrency)

        # Group by shared prefix to report cache hit rate
        prefix_groups: dict[str, int] = {}
        for req in requests:
            key = self._hash_prefix(req.shared_prefix)
            prefix_groups[key] = prefix_groups.get(key, 0) + 1
        print(f"[DEDUP] {len(requests)} requests, {len(prefix_groups)} unique prefixes")

        async def process_one(req: DeduplicatedRequest) -> DeduplicatedResult:
            async with sem:
                # Place shared prefix in system with cache_control
                system = [
                    {
                        "type": "text",
                        "text": req.shared_prefix,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                r = await client.messages.create(
                    model=self.model,
                    max_tokens=256,
                    system=system,
                    messages=[{"role": "user", "content": req.unique_suffix}],
                )
                u = r.usage
                cache_read = getattr(u, "cache_read_input_tokens", 0)
                return DeduplicatedResult(
                    request_id=req.request_id,
                    response=r.content[0].text,
                    shared_tokens_read_from_cache=cache_read,
                    unique_tokens_billed=u.input_tokens - cache_read,
                )

        results = await asyncio.gather(*[process_one(r) for r in requests])
        total_cached = sum(r.shared_tokens_read_from_cache for r in results)
        total_billed = sum(r.unique_tokens_billed for r in results)
        print(f"[DEDUP STATS] cached_reads={total_cached} unique_billed={total_billed} "
              f"cache_ratio={total_cached/(total_cached+total_billed+1):.1%}")
        return results


async def main():
    # Shared context: a large codebase description
    shared_ctx = (
        "You are reviewing a Python codebase. The codebase implements a REST API with the following structure:\n"
        "- /api/users: User management (CRUD)\n"
        "- /api/tasks: Task tracking with priorities and deadlines\n"
        "- /api/projects: Project hierarchy management\n"
        "The codebase uses FastAPI, SQLAlchemy, Pydantic v2, and PostgreSQL. "
        "Authentication uses JWT. All endpoints require authentication except /health.\n"
        "Database models: User(id, email, hashed_password, created_at), "
        "Task(id, title, description, priority, deadline, project_id, assignee_id), "
        "Project(id, name, description, owner_id, created_at).\n"
    ) * 5  # ~1500 tokens shared

    requests = [
        DeduplicatedRequest(shared_ctx, "What authentication method is used?", "req_1"),
        DeduplicatedRequest(shared_ctx, "List all database models and their fields.", "req_2"),
        DeduplicatedRequest(shared_ctx, "Which endpoint does not require authentication?", "req_3"),
        DeduplicatedRequest(shared_ctx, "What is the relationship between Task and Project?", "req_4"),
        DeduplicatedRequest(shared_ctx, "What framework is used for the REST API?", "req_5"),
    ]

    processor = DeduplicatedBatchProcessor()
    results = await processor.process_batch(requests)
    for r in results:
        print(f"[{r.request_id}] cached={r.shared_tokens_read_from_cache} billed={r.unique_tokens_billed}")
        print(f"  Answer: {r.response[:100]}\n")

asyncio.run(main())

# Expected Token Savings: With 5 calls sharing 1500 tokens: 4 cache reads save ~6000 tokens at 90% discount
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Adaptive Prefix Splitter for Optimal Cache Boundary

Automatically find the longest common prefix across a batch of messages and set the cache boundary at that point — maximizing cache hit rate without manual annotation.

```python
import anthropic
import asyncio
from dataclasses import dataclass

@dataclass
class CacheBoundary:
    common_prefix: str
    prefix_length: int
    unique_suffixes: list[str]
    estimated_savings_tokens: int

client = anthropic.AsyncAnthropic()

def find_longest_common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    shortest = min(strings, key=len)
    for i, char in enumerate(shortest):
        if not all(s[i] == char for s in strings):
            # Snap back to last newline for clean boundary
            prefix = shortest[:i]
            last_newline = prefix.rfind("\n")
            return prefix[:last_newline] if last_newline > 0 else prefix
    return shortest

def compute_cache_boundary(messages: list[str]) -> CacheBoundary:
    common = find_longest_common_prefix(messages)
    unique_suffixes = [m[len(common):].lstrip() for m in messages]
    # Savings: (n-1) × prefix_tokens × 0.9 discount
    prefix_tokens = len(common.split()) * 1.3  # rough token estimate
    savings = int((len(messages) - 1) * prefix_tokens * 0.9)
    return CacheBoundary(
        common_prefix=common,
        prefix_length=len(common),
        unique_suffixes=unique_suffixes,
        estimated_savings_tokens=savings,
    )

async def adaptive_cached_batch(
    prompts: list[str],
    model: str = "claude-haiku-4-5-20251001",
) -> list[str]:
    boundary = compute_cache_boundary(prompts)
    print(f"[ADAPTIVE CACHE] Common prefix: {boundary.prefix_length} chars, "
          f"~{boundary.estimated_savings_tokens} tokens saved")

    if not boundary.common_prefix:
        # No common prefix — fall back to individual calls
        tasks = [
            client.messages.create(
                model=model, max_tokens=256,
                messages=[{"role": "user", "content": p}],
            )
            for p in prompts
        ]
        results = await asyncio.gather(*tasks)
        return [r.content[0].text for r in results]

    # Use common prefix as cached system prompt
    system = [
        {
            "type": "text",
            "text": boundary.common_prefix,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    async def call_one(suffix: str) -> str:
        r = await client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": suffix}],
        )
        return r.content[0].text

    return await asyncio.gather(*[call_one(s) for s in boundary.unique_suffixes])


async def main():
    shared_context = (
        "You are analyzing the following dataset:\n"
        "| Month | Revenue | Users | Churn |\n"
        "|-------|---------|-------|-------|\n"
        "| Jan   | $120K   | 450   | 5%    |\n"
        "| Feb   | $135K   | 480   | 4%    |\n"
        "| Mar   | $148K   | 510   | 3.5%  |\n"
        "| Apr   | $162K   | 545   | 4%    |\n\n"
    )
    prompts = [
        shared_context + "What is the average monthly churn rate?",
        shared_context + "Which month had the highest revenue growth?",
        shared_context + "What is the total revenue for all four months?",
        shared_context + "Calculate user growth from January to April as a percentage.",
        shared_context + "What month showed the best churn improvement?",
    ]
    answers = await adaptive_cached_batch(prompts)
    for p, a in zip(prompts, answers):
        q = p.split("\n")[-1]
        print(f"Q: {q}\nA: {a[:100]}\n")

asyncio.run(main())

# Expected Token Savings: Auto-detects prefix; zero manual annotation; savings scale with batch size
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Multi-Layer Cache with System + Message Prefix Tiers

Use two cache layers: system prompt for static global context, and a cached user message for semi-static batch context. Unique per-request content in the final message.

```python
import anthropic
import asyncio
from dataclasses import dataclass

@dataclass
class TwoLayerResult:
    question: str
    answer: str
    layer1_cache_read: int  # system cache
    layer2_cache_read: int  # message cache

client = anthropic.AsyncAnthropic()

async def two_layer_cached_analysis(
    static_system_context: str,     # Layer 1: cached system (very stable, long-lived)
    batch_document_context: str,    # Layer 2: cached per batch (changes per job)
    questions: list[str],
    model: str = "claude-sonnet-4-6",
) -> list[TwoLayerResult]:
    # Layer 1: Static system instructions (global, reused across many batches)
    system = [
        {
            "type": "text",
            "text": static_system_context,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Layer 2: Batch-specific document (reused within this batch)
    shared_user_block = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": batch_document_context,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    shared_assistant_ack = {
        "role": "assistant",
        "content": "Document received. Ready for questions.",
    }

    async def ask(question: str) -> TwoLayerResult:
        messages = [
            shared_user_block,
            shared_assistant_ack,
            {"role": "user", "content": question},
        ]
        r = await client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=messages,
        )
        u = r.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0)
        # Rough attribution: system cache read vs message cache read
        system_toks = len(static_system_context.split()) * 1.3
        msg_toks = len(batch_document_context.split()) * 1.3
        layer1 = min(cache_read, int(system_toks))
        layer2 = max(0, cache_read - layer1)
        return TwoLayerResult(
            question=question,
            answer=r.content[0].text,
            layer1_cache_read=layer1,
            layer2_cache_read=layer2,
        )

    results = await asyncio.gather(*[ask(q) for q in questions])
    total_l1 = sum(r.layer1_cache_read for r in results)
    total_l2 = sum(r.layer2_cache_read for r in results)
    print(f"[2-LAYER CACHE] layer1_reads={total_l1} layer2_reads={total_l2}")
    return results


async def main():
    static_sys = (
        "You are a senior financial analyst with expertise in SaaS metrics, "
        "unit economics, cohort analysis, and revenue forecasting. "
        "Always show your calculations. Format numbers with appropriate units. "
        "If data is insufficient for a calculation, state what additional data is needed. "
        "Be precise and actionable in your recommendations."
    ) * 3  # Repeat to simulate larger static context

    batch_doc = (
        "Q3 2025 Performance Report:\n"
        "- ARR: $4.8M (up 32% YoY)\n"
        "- MRR: $400K\n"
        "- Net Revenue Retention: 118%\n"
        "- Gross Margin: 72%\n"
        "- CAC: $1,200 | LTV: $8,400 | LTV:CAC = 7.0\n"
        "- Monthly churn: 1.8%\n"
        "- New logo MRR: $45K | Expansion MRR: $28K | Churned MRR: $7.2K\n"
        "- Sales cycle avg: 28 days\n"
        "- Support tickets: 1,240 (avg resolution 4.2 hours)\n"
    ) * 4

    questions = [
        "What is the implied monthly growth rate given the ARR trajectory?",
        "Is our LTV:CAC ratio healthy? What is the benchmark?",
        "Calculate payback period based on CAC and gross margin.",
        "What does the NRR of 118% tell us about expansion vs churn?",
        "What is our net new MRR for this month?",
    ]

    results = await two_layer_cached_analysis(static_sys, batch_doc, questions)
    for r in results:
        print(f"Q: {r.question}\nA: {r.answer[:150]}\n")

asyncio.run(main())

# Expected Token Savings: Two cache tiers maximize hit rate; Layer 1 persists across batches, Layer 2 within batch
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Cache Layer | Automation | Use Case | Token Savings |
|--------|------------|------------|----------|--------------|
| 1. System Prompt Cache | System | Manual | Document Q&A with fixed doc | 80-90% on doc tokens |
| 2. Prefix-Shared Messages | User message | Manual | Multi-doc batch analysis | 85-90% on doc tokens |
| 3. Tool Definition Cache | Tool schema | Semi-auto | Multi-agent tool use | 90% on tool schema tokens |
| 4. Deduplicated Batch | System | Manual | Request factory pattern | Proportional to batch size |
| 5. Adaptive Prefix Splitter | System | Automatic | Ad-hoc batch with shared prefix | Auto-detected savings |
| 6. Two-Layer Cache | System + User | Manual | Large stable system + rotating docs | Maximum savings, two tiers |

**Recommended**: Option 5 (adaptive) for general use — zero annotation, automatic boundary detection. Option 6 for high-volume production systems with distinct static vs batch-specific context.
