---
layout: solution
title: "Agent sends identical system prompt in multi-agent fanout"
category: token-cost
description: "Orchestrator fans out to N subagents, each receiving a full copy of a 2000-token system prompt. With 10 subagents, the shared prefix is billed 10×. Prompt caching eliminates the redundant cost — the shared prefix is processed once and cached, reducing input cost by up to 90% on fanout patterns."
tags: [token-cost, multi-agent, fanout, prompt-caching, orchestration, system-prompt]
---

## Symptom

An orchestrator spawns 10 subagents in parallel, each with a 2000-token shared system prompt plus a small 100-token task-specific suffix. Total input tokens: 10 × 2100 = 21,000. Cost at Sonnet pricing: ~$0.063 per fanout batch. With prompt caching, the 2000-token shared prefix is billed once at full price then 9× at 10% of the price — reducing total to ~$0.027, a 57% reduction per batch.

## Root Cause

Each API call is stateless. Without explicit cache hints, the API processes the full system prompt for every subagent request. The orchestrator constructs each subagent's messages list independently, duplicating the shared prefix N times with no sharing of computation.

## Fix

Use prompt caching with `cache_control: {"type": "ephemeral"}` on the shared system prompt or the stable portion of the messages list. All N subagents in a fanout batch share the cache hit — the prefix is processed once, cached for 5 minutes, and the cache read costs 10% of the normal input price.

---

### Option 1 — Cached system prompt in parallel fanout

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(
    api_key="sk-live-...",
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

# Shared system prompt — large, stable, reused across all subagents
SHARED_SYSTEM = """You are a specialized research analyst. Your responsibilities:

1. Extract factual claims from the provided text
2. Identify the confidence level of each claim (high/medium/low)
3. Note any contradictions or inconsistencies
4. Flag claims that require external verification
5. Summarize key findings in structured format

Analysis framework:
- Primary sources > secondary sources > inference
- Quantitative claims require numeric evidence
- Temporal claims must include time reference
- Geographic claims must be geographically specific

Output format: JSON with fields: claims[], confidence_levels{}, contradictions[], verification_needed[], summary

Always be concise. Do not add prose outside the JSON structure.
Quality bar: every claim must be traceable to a specific passage in the input text.
""" * 3   # repeat to simulate a realistic large system prompt (~600 tokens)


async def run_subagent(task_text: str, task_id: int) -> dict:
    """Single subagent call with cached system prompt."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SHARED_SYSTEM,
                "cache_control": {"type": "ephemeral"},  # ← cache the shared prefix
            }
        ],
        messages=[{"role": "user", "content": task_text}],
    )
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(
        f"[Task {task_id}] tokens — input: {usage.input_tokens}, "
        f"cache_write: {cache_write}, cache_read: {cache_read}, "
        f"output: {usage.output_tokens}"
    )
    return {"task_id": task_id, "result": response.content[0].text}


async def run_fanout(tasks: list[str]) -> list[dict]:
    """Fan out to N subagents, all sharing the cached system prompt."""
    return await asyncio.gather(*[
        run_subagent(task, i) for i, task in enumerate(tasks)
    ])


TASKS = [
    "Analyze: 'Apple reported Q3 revenue of $81.8B, up 5% YoY.'",
    "Analyze: 'The study found 73% of participants improved, p<0.05.'",
    "Analyze: 'Temperature in Paris was 2°C above average last December.'",
    "Analyze: 'The merger is expected to close in Q1 2026, pending approval.'",
    "Analyze: 'Sales grew 12% in APAC while declining 3% in EMEA.'",
]

results = asyncio.run(run_fanout(TASKS))

# BEFORE: 5 tasks × 600-token system prompt = 3000 input tokens for shared prefix
# AFTER:  1 cache write (600 tok) + 4 cache reads (60 tok each) = 840 effective tokens
# Savings: ~72% on the shared prefix portion
```

**Expected Token Savings:** With 5 subagents and a 600-token shared system prompt: 1 cache write + 4 cache reads = 600 + 4×60 = 840 effective input tokens vs 5×600 = 3000 without caching — 72% reduction on the shared prefix.
**Environment:** Any multi-agent fanout pattern; the larger the shared system prompt and the more subagents, the higher the savings percentage.

---

### Option 2 — Cached shared context in the messages list

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(
    api_key="sk-live-...",
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

LARGE_SHARED_DOCUMENT = """
# Company Knowledge Base — Q4 2025

## Products
- Alpha Pro: enterprise SaaS, $500/seat/month, 2500 customers
- Beta Lite: SMB tier, $50/seat/month, 18000 customers
- Gamma API: pay-per-use, $0.002/call, 500M calls/month

## Support Policies
- SLA: P1 (1hr), P2 (4hr), P3 (24hr), P4 (72hr)
- Escalation: L1 → L2 → Engineering → CTO
- Refund policy: 30-day full refund, 90-day pro-rata

## Known Issues (as of 2025-12-01)
- Ticket #4821: Alpha Pro SSO intermittent on Okta tenants > 5000 users
- Ticket #5103: Beta Lite export CSV truncates at 50k rows
- Ticket #5287: Gamma API rate limit headers missing on 429 responses
""" * 5   # simulate a large shared knowledge base


async def run_support_subagent(customer_query: str, agent_id: int) -> str:
    """Support subagent — shared KB is cached, only the query differs."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="You are a customer support agent. Answer based only on the knowledge base provided.",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Knowledge Base:\n{LARGE_SHARED_DOCUMENT}",
                        "cache_control": {"type": "ephemeral"},  # ← cache the KB
                    },
                    {
                        "type": "text",
                        "text": f"\nCustomer query: {customer_query}",
                        # ← no cache_control on the dynamic part
                    },
                ],
            }
        ],
    )
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    print(f"[Agent {agent_id}] cache_read={cache_read} input={usage.input_tokens}")
    return response.content[0].text


async def handle_support_batch(queries: list[str]) -> list[str]:
    """Process a batch of support queries with shared KB caching."""
    return await asyncio.gather(*[
        run_support_subagent(q, i) for i, q in enumerate(queries)
    ])


queries = [
    "I'm on Alpha Pro and SSO keeps failing",
    "My CSV export is cutting off data",
    "I'm not seeing rate limit headers in API responses",
    "What's the refund policy for Beta Lite?",
    "Can you explain the P1 SLA guarantee?",
]

asyncio.run(handle_support_batch(queries))
```

**Expected Token Savings:** Shared knowledge base of ~1200 tokens: 5 subagents → 1 cache write + 4 cache reads = 1200 + 4×120 = 1680 effective tokens vs 5×1200 = 6000. Saves 72% on KB portion, ~40–50% on total per-call cost.
**Environment:** Support, Q&A, and RAG agents where a large shared document or knowledge base is sent to multiple parallel subagents.

---

### Option 3 — Orchestrator that batches subagents to maximize cache hits

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(
    api_key="sk-live-...",
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

SHARED_INSTRUCTIONS = """
You are a data extraction specialist. Extract structured information from the text.

Rules:
1. Extract only what is explicitly stated — no inference
2. Use null for missing fields, not empty strings
3. Dates must be ISO 8601 format
4. Numbers must be numeric types, not strings
5. Arrays must have at least one element or be null

Required fields: entity_name, entity_type, date, amount, currency, location, confidence_score (0-1)
""" * 4


class CachingFanoutOrchestrator:
    """
    Batches subagent calls to ensure cache is warm before firing the full fanout.
    Strategy: fire one "warmup" call first, wait for cache to be written,
    then fire the remaining N-1 calls as a batch.
    """
    def __init__(self, batch_size: int = 10):
        self.batch_size = batch_size

    async def _call_subagent(self, task: str, is_warmup: bool = False) -> tuple[str, dict]:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=[{
                "type": "text",
                "text": SHARED_INSTRUCTIONS,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": task}],
        )
        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_write = getattr(usage, "cache_creation_input_tokens", 0)

        tag = "WARMUP" if is_warmup else "BATCH"
        print(
            f"[{tag}] input={usage.input_tokens} "
            f"cache_write={cache_write} cache_read={cache_read} "
            f"output={usage.output_tokens}"
        )
        return response.content[0].text, {
            "input": usage.input_tokens,
            "cache_write": cache_write,
            "cache_read": cache_read,
            "output": usage.output_tokens,
        }

    async def run(self, tasks: list[str]) -> list[str]:
        if not tasks:
            return []

        results = []
        for batch_start in range(0, len(tasks), self.batch_size):
            batch = tasks[batch_start:batch_start + self.batch_size]

            # Fire first call to prime the cache
            first_result, _ = await self._call_subagent(batch[0], is_warmup=True)
            results.append(first_result)

            # Fire remaining calls — cache should be warm now
            if len(batch) > 1:
                remaining = await asyncio.gather(*[
                    self._call_subagent(t) for t in batch[1:]
                ])
                results.extend(r for r, _ in remaining)

        return results


orchestrator = CachingFanoutOrchestrator(batch_size=8)

tasks = [
    "Extract: 'Microsoft acquired Nuance for $19.7B in April 2022'",
    "Extract: 'Tesla reported $25.2B revenue in Q3 2023 in Austin, TX'",
    "Extract: 'Amazon opened a warehouse in Berlin, Germany on 2024-03-15'",
    "Extract: 'Apple sold 234M iPhones in FY2023 for $200.6B revenue'",
    "Extract: 'SpaceX launched Starship from Boca Chica on 2024-06-06'",
]

asyncio.run(orchestrator.run(tasks))
```

**Expected Token Savings:** Warmup strategy guarantees cache is written before the parallel batch fires, eliminating the race condition where N calls simultaneously miss the cache; for 8-call batches, reduces cache-write cost from up to 8× to exactly 1×.
**Environment:** High-throughput orchestrators; the warmup pattern is worth the 1-call sequential delay when N ≥ 4.

---

### Option 4 — Token cost accounting for fanout calls

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic(
    api_key="sk-live-...",
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

# Pricing per million tokens (Sonnet 4 approximate)
PRICE_INPUT = 3.00 / 1_000_000
PRICE_CACHE_WRITE = 3.75 / 1_000_000
PRICE_CACHE_READ = 0.30 / 1_000_000
PRICE_OUTPUT = 15.00 / 1_000_000


@dataclass
class FanoutCostTracker:
    batches: int = 0
    total_subagent_calls: int = 0
    total_input_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_output_tokens: int = 0

    def record(self, usage):
        self.total_subagent_calls += 1
        self.total_input_tokens += usage.input_tokens
        self.total_cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0)
        self.total_cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0)
        self.total_output_tokens += usage.output_tokens

    def cost(self) -> float:
        return (
            self.total_input_tokens * PRICE_INPUT
            + self.total_cache_write_tokens * PRICE_CACHE_WRITE
            + self.total_cache_read_tokens * PRICE_CACHE_READ
            + self.total_output_tokens * PRICE_OUTPUT
        )

    def cost_without_caching(self, shared_prompt_tokens: int) -> float:
        """Estimate what the cost would have been without caching."""
        hypothetical_input = (
            self.total_input_tokens
            + self.total_cache_read_tokens
            + self.total_cache_write_tokens
        )
        return hypothetical_input * PRICE_INPUT + self.total_output_tokens * PRICE_OUTPUT

    def report(self, shared_prompt_tokens: int) -> str:
        actual = self.cost()
        baseline = self.cost_without_caching(shared_prompt_tokens)
        savings = baseline - actual
        pct = savings / baseline * 100 if baseline > 0 else 0
        cache_hit_rate = (
            self.total_cache_read_tokens /
            max(self.total_cache_read_tokens + self.total_cache_write_tokens, 1) * 100
        )
        return (
            f"Fanout cost report — {self.total_subagent_calls} calls over {self.batches} batches\n"
            f"  Actual cost:   ${actual:.6f}\n"
            f"  Without cache: ${baseline:.6f}\n"
            f"  Savings:       ${savings:.6f} ({pct:.1f}%)\n"
            f"  Cache hit rate: {cache_hit_rate:.0f}%\n"
            f"  Tokens: input={self.total_input_tokens} "
            f"cwrite={self.total_cache_write_tokens} "
            f"cread={self.total_cache_read_tokens} "
            f"output={self.total_output_tokens}"
        )


tracker = FanoutCostTracker()
SHARED_PROMPT = "You are a summarization specialist. Summarize concisely. " * 20


async def tracked_subagent(task: str) -> str:
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=128,
        system=[{"type": "text", "text": SHARED_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": task}],
    )
    tracker.record(response.usage)
    return response.content[0].text


async def run_tracked_fanout(tasks: list[str]):
    tracker.batches += 1
    await asyncio.gather(*[tracked_subagent(t) for t in tasks])
    shared_tokens = len(SHARED_PROMPT) // 4
    print(tracker.report(shared_tokens))


asyncio.run(run_tracked_fanout([
    "Summarize: 'The cat sat on the mat.'",
    "Summarize: 'Revenue grew 15% YoY driven by cloud services.'",
    "Summarize: 'Python 3.13 released with improved JIT compilation.'",
    "Summarize: 'Team completed Q4 roadmap ahead of schedule.'",
]))
```

**Expected Token Savings:** The tracker quantifies exactly how much caching saves per fanout batch — use the report to validate that cache hits are actually occurring and measure ROI of the caching strategy.
**Environment:** Cost-sensitive production orchestrators; the tracker surfaces the real cost per batch, making it easy to justify and tune the caching strategy.

---

### Option 5 — Dynamic shared prefix with per-agent suffix injection

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(
    api_key="sk-live-...",
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

# Stable shared prefix — long, always the same, will be cached
STABLE_PREFIX = """
You are an expert legal document reviewer. Your task is to identify specific clauses
and flag risk areas in contract documents.

Standard analysis framework:
- Liability clauses: look for caps, indemnification, consequential damage waivers
- Termination clauses: notice periods, cure windows, automatic renewal traps
- IP ownership: work-for-hire language, license scope, sublicense rights
- Confidentiality: scope, duration, carve-outs, return/destruction obligations
- Governing law: jurisdiction, arbitration vs litigation, class action waiver
- Force majeure: scope (does it include pandemic, cyber?), notification requirements

Risk scoring: HIGH (immediate legal risk), MEDIUM (negotiate before signing), LOW (standard language)
Always output structured JSON: {clauses: [...], risks: [...], recommended_redlines: [...]}
""" * 4   # inflate to ~1000 tokens for realistic caching benefit


async def review_contract_clause(clause_text: str, clause_type: str) -> dict:
    """
    Subagent for a specific clause type.
    Shared prefix is cached; only the clause text varies per call.
    """
    task = f"Clause type: {clause_type}\n\nClause text:\n{clause_text}"

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{
            "type": "text",
            "text": STABLE_PREFIX,
            "cache_control": {"type": "ephemeral"},  # ← stable prefix cached
        }],
        messages=[{"role": "user", "content": task}],
    )
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
    return {
        "clause_type": clause_type,
        "analysis": response.content[0].text,
        "cache_hit": cache_read > 0,
    }


async def review_contract(contract_clauses: dict[str, str]) -> list[dict]:
    """Fan out to one subagent per clause type."""
    tasks = [
        review_contract_clause(text, clause_type)
        for clause_type, text in contract_clauses.items()
    ]
    return await asyncio.gather(*tasks)


contract = {
    "liability": "Vendor liability is capped at fees paid in the preceding 3 months.",
    "termination": "Either party may terminate with 30 days written notice.",
    "ip": "All work product created under this agreement is work-for-hire.",
    "confidentiality": "Recipient shall maintain confidentiality for 5 years post-termination.",
    "governing_law": "This agreement is governed by the laws of Delaware.",
}

results = asyncio.run(review_contract(contract))
for r in results:
    hit = "HIT" if r["cache_hit"] else "MISS"
    print(f"[{r['clause_type']}] Cache: {hit}")
```

**Expected Token Savings:** 5-clause contract review: 1 cache write + 4 cache reads on a 1000-token shared prefix = 1000 + 4×100 = 1400 effective vs 5000 without caching — 72% reduction on shared prefix alone.
**Environment:** Document review, legal analysis, code review pipelines that split a large document into multiple parallel analyses with a shared framework.

---

### Option 6 — Multi-tier caching: stable base + semi-stable layer

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(
    api_key="sk-live-...",
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

# Tier 1: Very stable base instructions (cached long-term)
BASE_INSTRUCTIONS = """
You are a financial data extraction agent. Extract structured data from financial reports.
Standard output schema: {company, period, revenue, operating_income, net_income,
eps_basic, eps_diluted, shares_outstanding, cash, debt, guidance_revenue, guidance_eps}
All monetary values in millions USD. Percentages as decimals. Dates as YYYY-Q# format.
""" * 6

# Tier 2: Semi-stable context (changes per batch but not per subagent)
def build_batch_context(sector: str, fiscal_year: str) -> str:
    return f"""
Current analysis batch:
- Sector: {sector}
- Fiscal year: {fiscal_year}
- Comparison baseline: Prior year same period
- Apply sector-specific adjustments: {"R&D capitalization" if sector == "technology" else "D&A normalization"}
"""


async def extract_financials(report_text: str, sector: str, fiscal_year: str) -> dict:
    """Two-tier cached prompt: base instructions + batch context."""
    batch_ctx = build_batch_context(sector, fiscal_year)

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": BASE_INSTRUCTIONS,
                "cache_control": {"type": "ephemeral"},  # ← tier 1: stable base
            },
            {
                "type": "text",
                "text": batch_ctx,
                "cache_control": {"type": "ephemeral"},  # ← tier 2: batch context
            },
        ],
        messages=[{"role": "user", "content": f"Report:\n{report_text}"}],
    )
    return {"result": response.content[0].text, "usage": response.usage}


async def process_earnings_batch(reports: list[str], sector: str = "technology", year: str = "2025") -> list[dict]:
    return await asyncio.gather(*[
        extract_financials(r, sector, year) for r in reports
    ])


# Comparison table
# | Option | Caching Target | N Subagents | Savings |
# |--------|---------------|------------|---------|
# | 1 System prompt | Shared instructions | 5 | ~72% on prefix |
# | 2 Messages context | Shared document/KB | 5 | ~72% on KB |
# | 3 Warmup strategy | Avoids cache miss race | N | 100% cache hit rate |
# | 4 Cost tracker | Measures cache ROI | any | Visibility |
# | 5 Dynamic suffix | Per-agent suffix + cached base | 5 | ~72% on stable base |
# | 6 Two-tier caching | Base + batch context | N | Two cache layers |

reports = [
    "Apple Q4 2025: Revenue $94.9B, Net Income $21.4B, EPS $1.40",
    "Google Q4 2025: Revenue $96.5B, Net Income $26.5B, EPS $2.15",
    "Microsoft Q4 2025: Revenue $69.6B, Net Income $24.7B, EPS $3.30",
]

asyncio.run(process_earnings_batch(reports))
```

**Expected Token Savings:** Two-tier caching compounds: tier 1 (600 tokens) + tier 2 (100 tokens) cached = 700 tokens read at 10% price on all but the first call; across 10 subagents per batch, total savings ~65–75% on the shared portions.
**Environment:** Orchestrators with a stable base + a per-batch context; the two-tier pattern maximizes cache coverage without sacrificing per-batch customization.
