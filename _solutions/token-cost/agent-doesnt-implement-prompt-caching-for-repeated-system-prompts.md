---
layout: solution
title: "Agent Doesn't Implement Prompt Caching for Repeated System Prompts"
category: token-cost
description: "Agents that re-send identical system prompts and tool definitions on every call pay full input token cost every time. These patterns show how to use Anthropic's prompt caching to cut repeated-context costs by up to 90%."
tags: [token-cost, prompt-caching, cache-control, optimization, cost, anthropic]
---

## Problem

A system prompt with 2,000 tokens of instructions, 3,000 tokens of tool definitions, and 5,000 tokens of reference documentation totals 10,000 input tokens per call. In a 50-turn conversation, that's 500,000 tokens — all for context that never changed. Anthropic's prompt caching lets you mark stable content blocks with `cache_control: {"type": "ephemeral"}`. On cache hit, you pay ~10% of the original input price for that block.

---

### Option 1: System Prompt Caching for Long-Running Conversations

Cache a large system prompt so it's only charged at full price on the first call.

```python
import anthropic

client = anthropic.Anthropic()

LARGE_SYSTEM_PROMPT = """You are an expert software architect with deep knowledge in:

## Core Competencies
- Distributed systems design (CAP theorem, consistency models, consensus algorithms)
- Database architecture (SQL, NoSQL, NewSQL, time-series, graph databases)
- API design (REST, GraphQL, gRPC, WebSockets, Server-Sent Events)
- Cloud-native patterns (microservices, serverless, event-driven architecture)
- Security (authentication, authorization, encryption, zero-trust)
- Performance engineering (caching strategies, CDN, connection pooling)
- Observability (distributed tracing, metrics, structured logging, SLOs)
- DevOps and CI/CD (blue-green deployments, canary releases, GitOps)

## Response Guidelines
- Always consider scalability implications
- Mention tradeoffs explicitly for every architectural decision
- Provide concrete examples with technology names
- Flag potential failure modes proactively
- Structure responses with clear headers and bullet points
- Include complexity and cost estimates where relevant

## Technology Stack Knowledge
- Cloud: AWS, GCP, Azure — services, pricing, regional differences
- Databases: PostgreSQL, MySQL, MongoDB, Cassandra, Redis, DynamoDB, BigQuery
- Messaging: Kafka, RabbitMQ, SQS, Pub/Sub, NATS
- Containers: Kubernetes, Docker, Helm, service meshes (Istio, Linkerd)
- Languages: Python, Go, Java, TypeScript — idiomatic patterns for each

Always cite specific services, not generic concepts, when recommending solutions."""  # ~250 words, ~350 tokens

def chat_with_system_cache(messages: list[dict]) -> tuple[str, dict]:
    """Send messages with cached system prompt. Returns (response_text, usage)."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": LARGE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # Cache this block
            }
        ],
        messages=messages,
    )
    usage = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_create": getattr(response.usage, "cache_creation_input_tokens", 0),
    }
    return response.content[0].text, usage

def run_conversation():
    history = []
    questions = [
        "How should I design a multi-region database for a fintech application?",
        "What caching strategy works best for the user profile service?",
        "How do I handle schema migrations without downtime?",
        "What observability setup would you recommend?",
    ]

    total_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    for q in questions:
        history.append({"role": "user", "content": q})
        reply, usage = chat_with_system_cache(history)
        history.append({"role": "assistant", "content": reply})

        for k in total_usage:
            total_usage[k] += usage[k]

        print(f"Q: {q[:50]}")
        print(f"  input={usage['input']} cache_read={usage['cache_read']} cache_create={usage['cache_create']}")

    print(f"\nTotal: {total_usage}")
    savings = total_usage["cache_read"] * 0.9  # ~90% discount on cache reads
    print(f"Estimated savings: ~{savings:.0f} token-equivalents")

if __name__ == "__main__":
    run_conversation()

# Expected Token Savings: 85-90% reduction on system prompt tokens after first call (cache_read at ~10% cost)
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Tool Definition Caching for Tool-Heavy Agents

Cache a large tool schema block so tool descriptions aren't re-charged on every turn.

```python
import anthropic

client = anthropic.Anthropic()

# Large tool definitions that would be expensive to re-send every turn
TOOLS = [
    {
        "name": "search_codebase",
        "description": "Search through the entire codebase for files, functions, classes, or patterns. Supports regex patterns and can filter by file type, directory, or modification date. Returns matching file paths, line numbers, and surrounding context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or regex pattern"},
                "file_type": {"type": "string", "description": "File extension filter (e.g. .py, .ts)"},
                "directory": {"type": "string", "description": "Subdirectory to search within"},
                "context_lines": {"type": "integer", "description": "Lines of context around matches", "default": 3},
                "max_results": {"type": "integer", "description": "Maximum results to return", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "execute_sql",
        "description": "Execute SQL queries against the production read replica. Supports SELECT, EXPLAIN, and SHOW statements. Automatically enforces a 30-second timeout and returns results as JSON. For large result sets, use LIMIT clauses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL query to execute (read-only)"},
                "database": {"type": "string", "description": "Target database name", "default": "production"},
                "timeout": {"type": "integer", "description": "Query timeout in seconds", "default": 30},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "call_api",
        "description": "Make HTTP requests to internal microservices. Handles authentication automatically using service account credentials. Supports GET, POST, PUT, PATCH, DELETE. Automatically retries on 5xx errors with exponential backoff.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name (e.g. user-service, billing-api)"},
                "path": {"type": "string", "description": "API path (e.g. /users/123)"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
                "body": {"type": "object", "description": "Request body for POST/PUT/PATCH"},
                "headers": {"type": "object", "description": "Additional headers"},
            },
            "required": ["service", "path"],
        },
    },
]

def agent_turn_with_tool_cache(messages: list[dict], system: str = "") -> dict:
    """Uses cache_control on the last tool to cache all preceding tool definitions."""
    # Mark the last tool with cache_control — this caches everything up to this point
    tools_with_cache = []
    for i, tool in enumerate(TOOLS):
        t = dict(tool)
        if i == len(TOOLS) - 1:
            t["cache_control"] = {"type": "ephemeral"}
        tools_with_cache.append(t)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        tools=tools_with_cache,
        messages=messages,
    )

    usage = {
        "input": response.usage.input_tokens,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_create": getattr(response.usage, "cache_creation_input_tokens", 0),
    }
    text = next((b.text for b in response.content if b.type == "text"), "")
    return {"text": text, "usage": usage, "stop_reason": response.stop_reason}

def run_tool_agent():
    history = []
    tasks = [
        "Search for all files that use the old authentication middleware.",
        "Query the database for users created in the last 30 days.",
        "Call the billing API to check account status for user 12345.",
        "Search for TODO comments in the codebase.",
    ]

    for task in tasks:
        history.append({"role": "user", "content": task})
        result = agent_turn_with_tool_cache(history, system="You are a developer assistant.")
        history.append({"role": "assistant", "content": result["text"] or "(tool call)"})

        u = result["usage"]
        print(f"Task: {task[:50]}")
        print(f"  input={u['input']} cache_read={u['cache_read']} cache_create={u['cache_create']}")

if __name__ == "__main__":
    run_tool_agent()

# Expected Token Savings: Tool definitions are often 1000-3000 tokens; caching saves ~90% after turn 1
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Multi-Block Caching — System + Documents + Tools

Cache multiple independent blocks: system instructions, reference documents, and tool schemas separately.

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM_INSTRUCTIONS = "You are a technical documentation assistant. Answer questions using the provided reference material. Be precise and cite specific sections."

REFERENCE_DOC = """# API Reference Documentation

## Authentication
All API requests require a Bearer token in the Authorization header.
Token expiry: 1 hour. Refresh using POST /auth/refresh.
Scopes: read, write, admin, billing.

## Rate Limits
Standard tier: 100 req/min, 10,000 req/day
Premium tier: 1,000 req/min, 500,000 req/day
Enterprise: custom limits, contact sales
Rate limit headers: X-RateLimit-Remaining, X-RateLimit-Reset

## Endpoints

### Users
GET /users — list users (paginated, max 100 per page)
POST /users — create user (requires write scope)
GET /users/{id} — get user by ID
PUT /users/{id} — update user
DELETE /users/{id} — soft delete (requires admin scope)

### Products
GET /products — list products with optional filters
POST /products — create product
GET /products/{id} — get product details including variants
PUT /products/{id}/inventory — update inventory count
POST /products/{id}/publish — publish product (requires write scope)

### Orders
GET /orders — list orders with filters (status, date_range, user_id)
POST /orders — create order
GET /orders/{id} — order details with line items
PUT /orders/{id}/status — update order status
POST /orders/{id}/refund — initiate refund (requires billing scope)

## Error Codes
400 Bad Request — invalid parameters (check error.details)
401 Unauthorized — missing or invalid token
403 Forbidden — insufficient scope
404 Not Found — resource does not exist
409 Conflict — resource already exists (check idempotency key)
429 Too Many Requests — rate limit exceeded (check Retry-After header)
500 Internal Server Error — contact support with request ID""" * 2  # ~700 tokens

LOOKUP_TOOLS = [
    {
        "name": "search_docs",
        "description": "Search the API documentation for specific endpoints, error codes, or concepts",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]

def multi_block_cached_call(user_message: str, history: list[dict]) -> tuple[str, dict]:
    system_blocks = [
        # Block 1: system instructions (shorter, may not always be worth caching)
        {
            "type": "text",
            "text": SYSTEM_INSTRUCTIONS,
        },
        # Block 2: large reference document (prime candidate for caching)
        {
            "type": "text",
            "text": REFERENCE_DOC,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Cache tool definitions too
    tools_with_cache = [
        {**t, "cache_control": {"type": "ephemeral"}} if i == len(LOOKUP_TOOLS) - 1 else t
        for i, t in enumerate(LOOKUP_TOOLS)
    ]

    messages = history + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system_blocks,
        tools=tools_with_cache,
        messages=messages,
    )

    usage = {
        "input": response.usage.input_tokens,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_create": getattr(response.usage, "cache_creation_input_tokens", 0),
        "output": response.usage.output_tokens,
    }
    text = next((b.text for b in response.content if b.type == "text"), "")
    return text, usage

def run_doc_assistant():
    history = []
    questions = [
        "What are the rate limits for the premium tier?",
        "How do I refresh an authentication token?",
        "What scope is required to delete a user?",
        "What does a 409 error mean?",
        "How do I paginate through users?",
    ]

    cumulative = {"input": 0, "cache_read": 0, "cache_create": 0, "output": 0}
    for q in questions:
        reply, usage = multi_block_cached_call(q, history)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": reply})
        for k in cumulative:
            cumulative[k] += usage[k]
        print(f"Q: {q[:50]}")
        print(f"  {usage}")

    print(f"\nCumulative: {cumulative}")
    effective_input = cumulative["input"] + cumulative["cache_create"] + cumulative["cache_read"] * 0.1
    print(f"Effective input (with cache discounts): ~{effective_input:.0f}")

if __name__ == "__main__":
    run_doc_assistant()

# Expected Token Savings: 80-90% on reference doc after first call; tools cached separately for addl savings
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Async Parallel Calls Sharing a Cached Prefix

Run multiple parallel requests that all benefit from the same cached system prefix.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

SHARED_ANALYSIS_CONTEXT = """You are a financial data analyst. Apply these analysis frameworks:

## SWOT Analysis Framework
- Strengths: internal positive factors under the organization's control
- Weaknesses: internal negative factors that require improvement
- Opportunities: external factors the organization can leverage
- Threats: external factors that could cause problems

## Financial Ratios to Consider
- Liquidity: Current ratio, Quick ratio, Cash ratio
- Profitability: Gross margin, Net margin, ROE, ROA, EBITDA margin
- Leverage: Debt-to-equity, Interest coverage, Debt-to-EBITDA
- Efficiency: Asset turnover, Inventory turnover, Days sales outstanding
- Valuation: P/E ratio, P/B ratio, EV/EBITDA, PEG ratio

## Analysis Standards
- Always provide quantitative benchmarks when available
- Compare against industry averages
- Flag any data quality concerns
- Distinguish between leading and lagging indicators
- Note cyclical vs secular trends
- Consider macroeconomic context

## Output Format
Provide structured analysis with: executive summary, key metrics, risks, opportunities, recommendation."""

async def analyze_company(company: str, question: str) -> tuple[str, dict]:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SHARED_ANALYSIS_CONTEXT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": f"Company: {company}\n\nQuestion: {question}"}],
    )
    usage = {
        "company": company,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_create": getattr(response.usage, "cache_creation_input_tokens", 0),
        "input": response.usage.input_tokens,
    }
    return response.content[0].text, usage

async def parallel_analysis(companies: list[str], question: str) -> list[tuple[str, dict]]:
    tasks = [analyze_company(company, question) for company in companies]
    return await asyncio.gather(*tasks)

if __name__ == "__main__":
    async def main():
        companies = ["Apple Inc", "Microsoft Corp", "Alphabet Inc", "Amazon.com"]
        question = "What are the key financial metrics I should evaluate for a long-term investment?"

        print(f"Analyzing {len(companies)} companies in parallel...\n")
        results = await parallel_analysis(companies, question)

        for (text, usage) in results:
            print(f"[{usage['company']}] input={usage['input']} "
                  f"cache_read={usage['cache_read']} cache_create={usage['cache_create']}")
            print(f"  {text[:150]}\n")

        total_cache_read = sum(r[1]["cache_read"] for r in results)
        print(f"Total cache_read_tokens: {total_cache_read} (paid at ~10% rate)")
    asyncio.run(main())

# Expected Token Savings: All 4 parallel calls share one cached prefix; ~90% savings on system prompt tokens
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Cache-Miss Detection and Warming Strategy

Detect cache misses and implement a warming strategy to ensure the cache is hot before high-load periods.

```python
import time
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

EXPENSIVE_CONTEXT = """[Imagine a 10,000-token legal document, policy manual, or codebase summary here.
For demo purposes this is shorter, but in production this would be your large stable content.]

## Legal Policy Manual - Data Processing Agreement

Section 1: Definitions and Scope
1.1 "Personal Data" means any information relating to an identified or identifiable natural person.
1.2 "Processing" means any operation performed on Personal Data.
1.3 "Data Controller" means the entity that determines the purposes and means of processing.
1.4 "Data Processor" means the entity that processes data on behalf of the controller.

Section 2: Obligations of the Data Processor
2.1 Process Personal Data only on documented instructions from the Controller.
2.2 Ensure that persons authorized to process Personal Data have committed to confidentiality.
2.3 Implement appropriate technical and organizational security measures.
2.4 Not engage another processor without prior written authorization from the Controller.

Section 3: Security Measures
3.1 Pseudonymization and encryption of Personal Data.
3.2 Ongoing confidentiality, integrity, availability of processing systems.
3.3 Ability to restore availability and access to Personal Data after incidents.
3.4 Regular testing and evaluation of security measures.

Section 4: Data Breach Notification
4.1 Notify Controller without undue delay after becoming aware of a breach.
4.2 Notification must include: nature of breach, contact point, likely consequences, measures taken.
4.3 Maintain records of all breaches regardless of notification obligation.""" * 3

async def call_with_cache_metrics(prompt: str) -> dict:
    start = time.monotonic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[
            {
                "type": "text",
                "text": EXPENSIVE_CONTEXT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.monotonic() - start
    return {
        "latency_ms": elapsed * 1000,
        "input": response.usage.input_tokens,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_create": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_hit": getattr(response.usage, "cache_read_input_tokens", 0) > 0,
    }

async def warm_cache(warming_prompt: str = "Summarize the key sections of this document.") -> dict:
    print("[warming cache...]")
    metrics = await call_with_cache_metrics(warming_prompt)
    print(f"[cache warm: create={metrics['cache_create']} tokens in {metrics['latency_ms']:.0f}ms]")
    return metrics

async def run_with_cache_strategy():
    # Warm the cache before the main workload
    warm_result = await warm_cache()
    assert warm_result["cache_create"] > 0, "Cache not created — content may be too short"

    # Now run the actual queries — all should be cache hits
    queries = [
        "What are the obligations of the Data Processor under Section 2?",
        "When must a Data Breach be notified under Section 4?",
        "What security measures are required under Section 3?",
    ]

    print(f"\nRunning {len(queries)} queries against warmed cache...")
    for q in queries:
        m = await call_with_cache_metrics(q)
        hit = "HIT" if m["cache_hit"] else "MISS"
        print(f"  [{hit}] {q[:50]}: latency={m['latency_ms']:.0f}ms cache_read={m['cache_read']}")

if __name__ == "__main__":
    asyncio.run(run_with_cache_strategy())

# Expected Token Savings: Cache warming ensures all production queries hit cache; prevents cold-start charges
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Dynamic Cache Block Selection Based on Content Stability

Automatically identify which content blocks are stable (cacheable) vs dynamic (not cacheable) using a stability classifier.

```python
import hashlib
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ContentBlock:
    content: str
    label: str
    change_frequency: str   # "never", "daily", "per_session", "per_request"

    @property
    def is_cacheable(self) -> bool:
        return self.change_frequency in ("never", "daily")

    @property
    def content_hash(self) -> str:
        return hashlib.md5(self.content.encode()).hexdigest()[:8]

    @property
    def estimated_tokens(self) -> int:
        return int(len(self.content.split()) * 1.35)

CONTENT_BLOCKS = [
    ContentBlock(
        label="system_instructions",
        content="You are a helpful customer support assistant for AcmeCorp software products.",
        change_frequency="never",
    ),
    ContentBlock(
        label="product_knowledge_base",
        content="""AcmeCorp Product Catalog:
        - AcmePro v4.2: Enterprise project management, 500 integrations, SOC2 compliant
        - AcmeLite v2.1: SMB solution, 50 integrations, 5GB storage limit
        - AcmeAPI v1.8: Developer SDK, REST and GraphQL, OpenAPI spec available
        Common issues: login (reset at /account/reset), billing (30-day refund policy),
        integrations (OAuth2 required), performance (use batch endpoints for >100 items)""",
        change_frequency="daily",
    ),
    ContentBlock(
        label="session_context",
        content=f"Current session started at {time.strftime('%H:%M')}. User is on the Pro plan.",
        change_frequency="per_session",
    ),
    ContentBlock(
        label="user_message_context",
        content="User's previous message: [varies per request]",
        change_frequency="per_request",
    ),
]

def build_cached_system(blocks: list[ContentBlock]) -> list[dict]:
    """Build system prompt with cache_control only on stable blocks."""
    result = []
    # Sort: cacheable blocks first (they form the stable prefix)
    cacheable = [b for b in blocks if b.is_cacheable]
    dynamic = [b for b in blocks if not b.is_cacheable]

    for i, block in enumerate(cacheable):
        entry = {"type": "text", "text": f"## {block.label}\n{block.content}"}
        # Mark the last cacheable block — caches everything up to here
        if i == len(cacheable) - 1:
            entry["cache_control"] = {"type": "ephemeral"}
        result.append(entry)

    # Dynamic blocks come after (not cached)
    for block in dynamic:
        result.append({"type": "text", "text": f"## {block.label}\n{block.content}"})

    return result

def support_call(user_message: str) -> tuple[str, dict]:
    system = build_cached_system(CONTENT_BLOCKS)

    cacheable_tokens = sum(b.estimated_tokens for b in CONTENT_BLOCKS if b.is_cacheable)
    dynamic_tokens = sum(b.estimated_tokens for b in CONTENT_BLOCKS if not b.is_cacheable)
    print(f"[cacheable≈{cacheable_tokens} tokens, dynamic≈{dynamic_tokens} tokens]")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    usage = {
        "input": response.usage.input_tokens,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_create": getattr(response.usage, "cache_creation_input_tokens", 0),
    }
    return response.content[0].text, usage

if __name__ == "__main__":
    tickets = [
        "How do I reset my password?",
        "What's included in the Pro plan?",
        "Can I get a refund?",
        "How do I set up the Slack integration?",
    ]
    for ticket in tickets:
        print(f"\nTicket: {ticket}")
        reply, usage = support_call(ticket)
        print(f"Reply: {reply[:150]}")
        print(f"Usage: {usage}")

# Expected Token Savings: Dynamic cache selection maximizes cacheable prefix; avoids caching volatile content
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | What Gets Cached | Cache Granularity | Best For |
|--------|-----------------|-------------------|----------|
| 1 | System prompt only | Single block | Long conversations with stable instructions |
| 2 | Tool definitions only | Tool list | Tool-heavy agents with large schemas |
| 3 | System + documents + tools | Multiple blocks | RAG agents with reference docs |
| 4 | Shared system prefix | Single block, parallel | Batch/parallel workloads sharing context |
| 5 | Full context with warm-up | Single block | Predictable high-load windows |
| 6 | Dynamic stability-based selection | Auto-selected blocks | Mixed stable/dynamic content systems |
