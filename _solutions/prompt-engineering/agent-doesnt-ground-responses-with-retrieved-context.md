---
layout: solution
title: "Agent doesn't ground responses with retrieved context"
category: prompt-engineering
description: "Agent answers questions from parametric memory alone, producing confident but hallucinated answers when domain-specific or recent information is needed."
tags: [prompt-engineering, rag, grounding, retrieval, hallucination]
---

## Symptom

The agent answers product questions, policy queries, or technical questions from training data alone. The answers sound plausible but contain outdated information, invented details, or flat-out wrong specifics. Users report factual errors. When the same question is asked against the company knowledge base it gets a different (correct) answer.

```
User: "What is the cancellation policy for Enterprise plan?"
Agent: "Enterprise customers can cancel at any time with 30 days notice."
Actual policy: "Enterprise contracts require 90 days written notice."
```

## Root Cause

The agent is not instructed to retrieve relevant documents before answering. The model answers from training-time pattern matching rather than from authoritative sources. This is the core failure mode that retrieval-augmented generation (RAG) is designed to solve.

## Fix

Before generating an answer, retrieve relevant documents using a search tool, inject them into the prompt as grounding context, and instruct the model to answer only from that context — flagging gaps rather than guessing.

---

### Option 1 — Basic RAG: retrieve then answer

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulated knowledge base (in production: vector DB, BM25, or hybrid search)
KNOWLEDGE_BASE = [
    {"id": "kb-001", "title": "Enterprise Plan — Cancellation Policy",
     "content": "Enterprise customers must provide 90 days written notice to cancel. "
                "Cancellation requests must be sent to enterprise-contracts@example.com. "
                "Refunds are not issued for the notice period."},
    {"id": "kb-002", "title": "Starter Plan — Pricing",
     "content": "Starter plan costs $29/month billed monthly or $290/year. "
                "Includes up to 5 users and 10GB storage."},
    {"id": "kb-003", "title": "Data Export",
     "content": "All plans support CSV and JSON data export. Enterprise plans also "
                "support direct database replication to customer-hosted PostgreSQL."},
    {"id": "kb-004", "title": "SLA — Uptime Guarantee",
     "content": "Enterprise SLA guarantees 99.95% uptime. Starter and Pro plans "
                "have a 99.9% uptime SLA. Credits are issued for outages exceeding the SLA."},
]

def search_knowledge_base(query: str, top_k: int = 3) -> list[dict]:
    """Keyword search — replace with vector similarity search in production."""
    query_words = set(query.lower().split())
    scored = []
    for doc in KNOWLEDGE_BASE:
        text = (doc["title"] + " " + doc["content"]).lower()
        score = sum(1 for w in query_words if w in text)
        if score > 0:
            scored.append((score, doc))
    scored.sort(reverse=True)
    return [doc for _, doc in scored[:top_k]]

def grounded_answer(question: str) -> dict:
    # Step 1: Retrieve relevant context
    docs = search_knowledge_base(question)

    if not docs:
        context = "No relevant documents found in the knowledge base."
    else:
        context = "\n\n".join(
            f"[{d['id']}] {d['title']}\n{d['content']}" for d in docs
        )

    # Step 2: Answer from retrieved context only
    system = (
        "You are a customer support agent. Answer questions using ONLY the provided "
        "context documents. If the context does not contain the answer, say: "
        "'I don't have that information — please contact support.' "
        "Never guess or use outside knowledge. Cite the document ID."
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{
            "role": "user",
            "content": f"Context documents:\n{context}\n\nQuestion: {question}",
        }],
    )

    return {
        "question":  question,
        "answer":    response.content[0].text.strip(),
        "docs_used": [d["id"] for d in docs],
        "tokens":    response.usage.input_tokens + response.usage.output_tokens,
    }

questions = [
    "What is the cancellation policy for Enterprise plan?",
    "How much does the Starter plan cost?",
    "Can I export my data?",
    "What is your refund policy for monthly plans?",  # not in KB
]

for q in questions:
    result = grounded_answer(q)
    print(f"Q: {q}")
    print(f"A: {result['answer'][:120]}")
    print(f"Docs: {result['docs_used']} | Tokens: {result['tokens']}\n")
```

**Expected Token Savings:** Grounding eliminates hallucination-induced re-work; retrieval tokens (~500) replace the alternative (expensive multi-turn correction cycles) saving 60–80% overall.

**Environment:** Any domain-specific Q&A agent; replace the keyword search with a vector database (Pinecone, Weaviate, pgvector) for semantic retrieval.

---

### Option 2 — Tool-based RAG: agent decides when to retrieve

```python
import anthropic
import json

client = anthropic.Anthropic()

DOCS = {
    "pricing":     "Pro plan: $79/month. Enterprise: custom pricing from sales.",
    "sla":         "Pro SLA: 99.9% uptime. Enterprise SLA: 99.95% with credits.",
    "cancel":      "Pro plan: cancel anytime, effective end of billing period. "
                   "Enterprise: 90 days written notice required.",
    "integrations":"Supports Slack, Jira, GitHub, Salesforce. Enterprise adds SAP, Oracle.",
    "security":    "SOC 2 Type II certified. Data encrypted at rest (AES-256) and in transit (TLS 1.3).",
}

def search_docs(query: str) -> str:
    query_lower = query.lower()
    results = {}
    for key, content in DOCS.items():
        if any(w in (key + " " + content).lower() for w in query_lower.split()):
            results[key] = content
    if not results:
        return json.dumps({"found": False, "message": "No matching documents."})
    return json.dumps({"found": True, "documents": results})

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the company knowledge base for accurate, up-to-date information. "
            "Always use this tool before answering questions about pricing, policies, "
            "features, or SLAs. Do NOT answer from memory alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — use relevant keywords from the user's question.",
                }
            },
            "required": ["query"],
        },
    }
]

SYSTEM = (
    "You are a helpful support agent. For any factual question about the product "
    "(pricing, policies, features, SLAs, integrations), you MUST call "
    "search_knowledge_base first and base your answer on the results. "
    "Never answer product questions from memory."
)

def ask(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            result = search_docs(b.input.get("query", ""))
            print(f"  [SEARCH] '{b.input.get('query')}' → {result[:80]}")
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})

        messages.append({"role": "user", "content": results})

    return next(b.text for b in response.content if hasattr(b, "text"))

for q in [
    "What's the difference in SLA between Pro and Enterprise?",
    "Does your product integrate with Salesforce?",
    "How do I cancel my Pro subscription?",
]:
    print(f"\nQ: {q}")
    print(f"A: {ask(q)}")
```

**Expected Token Savings:** Agent retrieves only when needed; the `search_knowledge_base` instruction ensures no question is answered from parametric memory; tool-based approach scales to hundreds of documents.

**Environment:** Conversational support agents; the tool instruction forces retrieval discipline more reliably than system-prompt instructions alone.

---

### Option 3 — Cite-or-abstain grounding with confidence gating

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

KNOWLEDGE = [
    {"id": "P-1", "topic": "pricing",     "text": "Teams plan: $49/user/month, minimum 3 users."},
    {"id": "P-2", "topic": "trial",       "text": "Free 14-day trial, no credit card required."},
    {"id": "P-3", "topic": "support",     "text": "Email support for all plans. Live chat for Pro+. "
                                                    "Dedicated CSM for Enterprise."},
    {"id": "P-4", "topic": "compliance",  "text": "GDPR compliant. HIPAA BAA available on Enterprise only."},
]

def retrieve(query: str) -> list[dict]:
    words = set(query.lower().split())
    return [d for d in KNOWLEDGE if any(w in d["text"].lower() or w in d["topic"] for w in words)]

CITE_OR_ABSTAIN_SYSTEM = """
Answer the question using ONLY the provided context. Rules:
1. Cite sources using [ID] notation after each factual claim.
2. If the context fully answers the question, answer it.
3. If the context partially answers, answer what you can and note gaps.
4. If the context has NO relevant information, respond exactly:
   ABSTAIN: [brief reason why context is insufficient]
Never fabricate information not in the context.
""".strip()

def grounded_with_abstain(question: str) -> dict:
    docs = retrieve(question)
    context = "\n".join(f"[{d['id']}] {d['text']}" for d in docs) if docs else "(no documents retrieved)"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CITE_OR_ABSTAIN_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}",
        }],
    )
    answer = response.content[0].text.strip()
    abstained = answer.upper().startswith("ABSTAIN")
    citations  = re.findall(r"\[([A-Z]-\d+)\]", answer)

    return {
        "answer":    answer,
        "abstained": abstained,
        "citations": citations,
        "docs_retrieved": len(docs),
    }

for q in [
    "Is HIPAA compliance available?",
    "How much does the Teams plan cost per user?",
    "What is the refund policy?",   # not in KB — should abstain
    "Do you offer a free trial?",
]:
    result = grounded_with_abstain(q)
    status = "ABSTAIN" if result["abstained"] else f"CITED: {result['citations']}"
    print(f"Q: {q}")
    print(f"A: {result['answer'][:100]}")
    print(f"   [{status}] docs_retrieved={result['docs_retrieved']}\n")
```

**Expected Token Savings:** Abstain pattern eliminates follow-up correction turns; explicit citations allow downstream systems to verify claims without re-querying the model.

**Environment:** High-stakes support or legal-adjacent agents where fabricated answers are costly; abstain responses trigger escalation to a human.

---

### Option 4 — Async multi-query retrieval for complex questions

```python
import anthropic
import asyncio
import json

async_client = anthropic.AsyncAnthropic()

DOCS = {
    "billing":    "Invoices are issued on the 1st of each month. Payment terms: Net 30.",
    "upgrade":    "Plan upgrades take effect immediately. You are charged the prorated difference.",
    "downgrade":  "Downgrades take effect at the start of the next billing cycle.",
    "refund":     "Refunds are issued within 5 business days to the original payment method.",
    "storage":    "Starter: 10GB. Pro: 100GB. Enterprise: unlimited.",
    "api_limits": "Starter: 1000 API calls/day. Pro: 50000/day. Enterprise: custom.",
}

async def decompose_question(question: str) -> list[str]:
    """Break a complex question into sub-queries for better retrieval coverage."""
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Break this question into 2-3 focused sub-queries for document retrieval. "
                f"Return JSON: {{\"queries\": [\"query1\", \"query2\", ...]}}\n\nQuestion: {question}"
            ),
        }],
    )
    raw = resp.content[0].text
    start, end = raw.find("{"), raw.rfind("}") + 1
    return json.loads(raw[start:end]).get("queries", [question])

async def retrieve_for_query(query: str) -> list[str]:
    words = set(query.lower().split())
    return [content for key, content in DOCS.items()
            if any(w in key or w in content.lower() for w in words)]

async def multi_query_rag(question: str) -> str:
    # Decompose and retrieve in parallel
    sub_queries = await decompose_question(question)
    print(f"Sub-queries: {sub_queries}")

    retrieval_tasks = [retrieve_for_query(q) for q in sub_queries]
    all_results = await asyncio.gather(*retrieval_tasks)

    # Deduplicate and merge context
    seen = set()
    merged = []
    for results in all_results:
        for r in results:
            if r not in seen:
                merged.append(r)
                seen.add(r)

    context = "\n".join(f"- {r}" for r in merged) or "No relevant documents found."

    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            "Answer using ONLY the provided context. "
            "If context is insufficient, say what is missing."
        ),
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}",
        }],
    )
    return resp.content[0].text.strip()

async def main() -> None:
    questions = [
        "If I upgrade from Starter to Pro mid-month and then downgrade next month, what happens to billing and storage?",
        "What are the API limits and how does storage scale across plans?",
    ]
    for q in questions:
        print(f"\nQ: {q}")
        answer = await multi_query_rag(q)
        print(f"A: {answer}")

asyncio.run(main())
```

**Expected Token Savings:** Multi-query retrieval finds relevant context that single-query approaches miss; parallel async retrieval adds minimal latency; better context means fewer correction turns downstream.

**Environment:** Complex, multi-faceted questions common in enterprise support; decomposition adds one small API call but dramatically improves retrieval recall.

---

### Option 5 — Grounding with source freshness metadata

```python
import anthropic
import json
import time

client = anthropic.Anthropic()

# Documents with staleness metadata
DOCS = [
    {
        "id": "D-1", "title": "API Rate Limits",
        "content": "Free tier: 100 req/min. Pro: 1000 req/min. Enterprise: 10000 req/min.",
        "updated_epoch": 1714000000,   # recent
        "confidence": "authoritative",
    },
    {
        "id": "D-2", "title": "Legacy Pricing (deprecated)",
        "content": "Pro plan was $49/month before the 2024 price change.",
        "updated_epoch": 1680000000,   # old
        "confidence": "deprecated",
    },
    {
        "id": "D-3", "title": "Current Pricing",
        "content": "Pro plan: $79/month. Starter: $29/month. Enterprise: contact sales.",
        "updated_epoch": 1712000000,   # recent
        "confidence": "authoritative",
    },
]

def retrieve_with_freshness(query: str) -> list[dict]:
    now = time.time()
    query_words = set(query.lower().split())
    scored = []
    for doc in DOCS:
        text = (doc["title"] + " " + doc["content"]).lower()
        relevance = sum(1 for w in query_words if w in text)
        if relevance == 0:
            continue
        age_days = (now - doc["updated_epoch"]) / 86400
        freshness_score = 1.0 if age_days < 90 else 0.5 if age_days < 365 else 0.1
        scored.append((relevance * freshness_score, doc))
    scored.sort(reverse=True)
    return [d for _, d in scored[:3]]

def answer_with_freshness_context(question: str) -> str:
    docs = retrieve_with_freshness(question)
    if not docs:
        context = "No relevant documents found."
    else:
        parts = []
        for d in docs:
            age = (time.time() - d["updated_epoch"]) / 86400
            freshness = "CURRENT" if age < 90 else f"POSSIBLY OUTDATED ({age:.0f} days old)"
            parts.append(
                f"[{d['id']} — {freshness} — {d['confidence'].upper()}]\n{d['content']}"
            )
        context = "\n\n".join(parts)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            "Answer using the provided documents. Prefer CURRENT and authoritative sources. "
            "If a document is marked POSSIBLY OUTDATED or deprecated, warn the user and "
            "recommend they verify with official sources."
        ),
        messages=[{
            "role": "user",
            "content": f"Documents:\n{context}\n\nQuestion: {question}",
        }],
    )
    return response.content[0].text.strip()

for q in ["What does the Pro plan cost?", "What are the API rate limits?"]:
    print(f"Q: {q}")
    print(f"A: {answer_with_freshness_context(q)}\n")
```

**Expected Token Savings:** Freshness scoring reduces reliance on stale documents; explicit staleness warnings in context prevent the model from presenting outdated facts confidently; fewer correction turns.

**Environment:** Knowledge bases that mix current and archived documents; any agent where document staleness matters (pricing, regulatory, technical specs).

---

### Option 6 — Reranking retrieved documents before injection

```python
import anthropic
import json

client = anthropic.Anthropic()

CANDIDATE_DOCS = [
    {"id": "R-1", "content": "Return policy: items can be returned within 30 days."},
    {"id": "R-2", "content": "Enterprise contracts: 90-day cancellation notice required."},
    {"id": "R-3", "content": "Subscription billing runs monthly on the anniversary date."},
    {"id": "R-4", "content": "Refunds for annual plans are prorated for unused months."},
    {"id": "R-5", "content": "Team members can be added at any time; billed prorated."},
    {"id": "R-6", "content": "Downgrading a plan takes effect at the next billing cycle."},
]

def first_pass_retrieve(query: str, top_k: int = 4) -> list[dict]:
    """Broad keyword retrieval — high recall, lower precision."""
    words = set(query.lower().split())
    scored = [
        (sum(1 for w in words if w in d["content"].lower()), d)
        for d in CANDIDATE_DOCS
    ]
    scored.sort(reverse=True)
    return [d for _, d in scored[:top_k] if _ > 0]

def rerank_with_llm(query: str, candidates: list[dict]) -> list[dict]:
    """LLM reranker: score each candidate for relevance to the query."""
    if not candidates:
        return []

    numbered = "\n".join(f"{i}: [{d['id']}] {d['content']}" for i, d in enumerate(candidates))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"Rate each document's relevance to the query on a scale 0-10.\n"
                f"Query: {query}\n\nDocuments:\n{numbered}\n\n"
                f"Return JSON: {{\"scores\": [score0, score1, ...]}} (same order as documents)"
            ),
        }],
    )
    raw = response.content[0].text
    start, end = raw.find("{"), raw.rfind("}") + 1
    scores = json.loads(raw[start:end]).get("scores", [5] * len(candidates))
    reranked = sorted(zip(scores, candidates), reverse=True)
    return [d for score, d in reranked if score >= 5]

def rag_with_reranking(question: str) -> str:
    # Step 1: broad retrieval
    candidates = first_pass_retrieve(question, top_k=4)
    print(f"First pass: {[d['id'] for d in candidates]}")

    # Step 2: rerank
    top_docs = rerank_with_llm(question, candidates)
    print(f"After rerank: {[d['id'] for d in top_docs]}")

    if not top_docs:
        context = "No relevant documents found."
    else:
        context = "\n".join(f"[{d['id']}] {d['content']}" for d in top_docs)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="Answer using ONLY the provided context. Cite document IDs.",
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}],
    )
    return response.content[0].text.strip()

for q in [
    "Can I get a refund if I cancel my annual plan mid-year?",
    "How does billing work when I add a new team member?",
]:
    print(f"\nQ: {q}")
    print(f"A: {rag_with_reranking(q)}")
```

**Expected Token Savings:** Reranking narrows context to 1–2 high-relevance documents instead of injecting all 4 retrieved; reduces context tokens by 40–60% while improving answer accuracy.

**Environment:** Knowledge bases with many similar-sounding documents; reranking prevents the "lost in the middle" effect where the model ignores documents buried in a long context.

---

## Comparison

| Option | Retrieval Method | Hallucination Control | Abstain Capable | Best For |
|--------|----------------|----------------------|----------------|---------|
| 1 — Basic RAG | Keyword search | Context-only instruction | No | Getting started |
| 2 — Tool-based | Agent-driven | Tool instruction | No | Conversational agents |
| 3 — Cite-or-abstain | Keyword | Abstain instruction | Yes | High-stakes Q&A |
| 4 — Multi-query | Parallel async | Context-only | No | Complex questions |
| 5 — Freshness-aware | Scored retrieval | Freshness warning | No | Evolving knowledge bases |
| 6 — Reranking | Keyword + LLM | Context-only | No | Large document collections |

**Recommended default:** Option 2 (tool-based) — the tool instruction enforces retrieval discipline better than system-prompt instructions alone, and it composes naturally with other tools the agent uses.
