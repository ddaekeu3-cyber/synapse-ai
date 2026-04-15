---
layout: solution
title: "Agent Doesn't Filter Irrelevant Documents Before Injection"
category: context-window
description: "Agent injects all retrieved documents into the context window regardless of their relevance score, bloating the prompt with noise that dilutes the model's attention to the actually relevant passages and wastes tokens on useless content."
tags: [context-window, rag, retrieval, relevance-filtering, embeddings]
---

## Symptom

The agent retrieves 10 documents and injects all of them — but 7 are largely irrelevant:

```
Query: "How do I configure SMTP for password reset emails?"

Retrieved docs (cosine similarity):
  doc-1: 0.91 — "SMTP configuration guide"              ← relevant
  doc-2: 0.73 — "Password reset flow overview"          ← relevant
  doc-3: 0.61 — "Email delivery troubleshooting"        ← marginal
  doc-4: 0.52 — "Authentication middleware setup"       ← irrelevant
  doc-5: 0.48 — "Database schema for users table"       ← irrelevant
  ...
  doc-10: 0.31 — "Pricing FAQ"                          ← irrelevant

Total injected: 8,200 tokens  (6,100 tokens are irrelevant noise)
```

The model's attention is split across all 10 documents. It produces an answer that mixes SMTP configuration with unrelated authentication details.

## Root Cause

The retriever returns top-K results and all K documents are injected without any threshold filter:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def rag_query(query: str, docs: list[dict]) -> str:
    # Retrieve top 10 — no relevance threshold applied
    top_k = sorted(docs, key=lambda d: d["score"], reverse=True)[:10]

    # ALL 10 injected regardless of score
    context = "\n\n".join(d["text"] for d in top_k)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text
```

---

## Fix

### Option 1 — Score threshold filter

Only inject documents above a minimum similarity score. Below the threshold, the document is excluded regardless of K.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

MIN_RELEVANCE_SCORE = 0.65  # Cosine similarity threshold
MAX_DOCS = 5                # Hard cap even if many pass threshold


def rag_query(query: str, docs: list[dict]) -> str:
    # Filter by threshold first, then cap at MAX_DOCS
    relevant = [
        d for d in sorted(docs, key=lambda d: d["score"], reverse=True)
        if d["score"] >= MIN_RELEVANCE_SCORE
    ][:MAX_DOCS]

    if not relevant:
        # No relevant docs — tell the model explicitly
        context = "[No relevant documentation found for this query.]"
    else:
        context = "\n\n---\n\n".join(
            f"[Relevance: {d['score']:.2f}]\n{d['text']}"
            for d in relevant
        )
        print(f"Injecting {len(relevant)}/{len(docs)} docs "
              f"(filtered {len(docs)-len(relevant)} below {MIN_RELEVANCE_SCORE:.0%} threshold)")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


# Simulated retrieval results
docs = [
    {"text": "SMTP config: host=smtp.example.com port=587...", "score": 0.91},
    {"text": "Password reset sends email via configured SMTP...", "score": 0.73},
    {"text": "Database schema: users(id, email, password_hash)...", "score": 0.48},
    {"text": "Pricing: starter plan $9/month...", "score": 0.31},
]

print(rag_query("How do I configure SMTP for password resets?", docs))

# Expected Token Savings: 2 docs × ~400 tokens each = 800 tokens saved vs injecting all 4
# Environment: any RAG pipeline using vector similarity search
```

---

### Option 2 — Reranking with a cross-encoder before injection

Use a lightweight cross-encoder to rerank retrieved documents based on query-document relevance. Cross-encoders are far more accurate than embedding cosine similarity.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def rerank_with_haiku(query: str, docs: list[dict], top_n: int = 3) -> list[dict]:
    """
    Use Haiku as a lightweight reranker. Score each doc's relevance to the query.
    In production: use a dedicated reranker (Cohere rerank, BGE reranker, etc.)
    """
    scored = []
    for doc in docs:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": f"""Rate how relevant this document is to the query.
Query: {query}
Document (first 200 chars): {doc['text'][:200]}
Relevance score 0-10 (integer only):"""
            }]
        )
        try:
            score = int(response.content[0].text.strip())
        except ValueError:
            score = 0
        scored.append({**doc, "rerank_score": score})

    # Sort by rerank score, return top N
    reranked = sorted(scored, key=lambda d: d["rerank_score"], reverse=True)
    filtered = [d for d in reranked[:top_n] if d["rerank_score"] >= 5]  # Min score 5/10
    return filtered


def rag_with_reranking(query: str, docs: list[dict]) -> str:
    relevant = rerank_with_haiku(query, docs, top_n=3)

    if not relevant:
        return "I don't have relevant documentation for this question."

    context = "\n\n---\n\n".join(
        f"[Rerank score: {d['rerank_score']}/10]\n{d['text']}"
        for d in relevant
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


docs = [
    {"text": "SMTP setup: host=smtp.example.com, port=587, auth=PLAIN...", "score": 0.91},
    {"text": "User table schema: id INT, email VARCHAR(255)...", "score": 0.52},
    {"text": "Password reset email triggered on /forgot-password endpoint...", "score": 0.73},
    {"text": "Monthly pricing starts at $9 per seat...", "score": 0.31},
]

print(rag_with_reranking("How do I configure SMTP for password reset emails?", docs))

# Expected Token Savings: reranking keeps 2–3 docs vs 10; Haiku cost ~50 tokens per doc scored
# Environment: high-quality RAG where retrieval precision matters more than speed
```

---

### Option 3 — Token-budget-aware document selection

Select documents greedily until a token budget is consumed. Higher-scoring documents are included first; lower-scoring ones are excluded when budget runs out.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

CONTEXT_TOKEN_BUDGET = 3000  # Reserve for system + question + response
TOKENS_PER_CHAR = 0.25        # Rough estimate: 1 token ≈ 4 chars


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) * TOKENS_PER_CHAR))


def select_within_budget(
    docs: list[dict],
    budget: int = CONTEXT_TOKEN_BUDGET,
    min_score: float = 0.55,
) -> tuple[list[dict], int]:
    """Select highest-scoring docs that fit within token budget."""
    sorted_docs = sorted(
        [d for d in docs if d["score"] >= min_score],
        key=lambda d: d["score"],
        reverse=True,
    )

    selected = []
    used_tokens = 0

    for doc in sorted_docs:
        doc_tokens = estimate_tokens(doc["text"])
        if used_tokens + doc_tokens <= budget:
            selected.append(doc)
            used_tokens += doc_tokens
        else:
            print(f"[budget] Skipping doc (score={doc['score']:.2f}, "
                  f"tokens~{doc_tokens}) — budget exhausted ({used_tokens}/{budget})")

    return selected, used_tokens


def rag_budget_aware(query: str, docs: list[dict]) -> str:
    selected, token_count = select_within_budget(docs)

    if not selected:
        context = "[No relevant documents found within token budget.]"
    else:
        context = "\n\n---\n\n".join(d["text"] for d in selected)
        print(f"[budget] Using {len(selected)} docs, ~{token_count} tokens")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


docs = [
    {"text": "A" * 4000, "score": 0.91},   # High score, large doc
    {"text": "B" * 2000, "score": 0.83},   # High score, medium doc
    {"text": "C" * 1500, "score": 0.71},   # Medium score, fits
    {"text": "D" * 800,  "score": 0.52},   # Below threshold
]

print(rag_budget_aware("test query", docs))

# Expected Token Savings: hard token budget guarantees total context size; prevents 429 from
#   context-too-long errors on large document sets
# Environment: RAG with variable-length documents where context overflow is a risk
```

---

### Option 4 — Marginal relevance filtering (MMR) to reduce redundancy

Use Maximal Marginal Relevance to select documents that are both relevant AND diverse — avoiding 3 near-identical documents that waste tokens repeating the same content.

```python
import anthropic
import math

client = anthropic.Anthropic(api_key="sk-live-...")


def simple_overlap(a: str, b: str) -> float:
    """Token overlap similarity (replace with embedding cosine in production)."""
    a_tok = set(a.lower().split())
    b_tok = set(b.lower().split())
    if not a_tok or not b_tok:
        return 0.0
    return len(a_tok & b_tok) / math.sqrt(len(a_tok) * len(b_tok))


def mmr_select(
    docs: list[dict],
    top_k: int = 4,
    lambda_param: float = 0.6,  # 0 = max diversity, 1 = max relevance
    min_score: float = 0.55,
) -> list[dict]:
    """
    Maximal Marginal Relevance selection.
    Balances relevance to query with diversity among selected docs.
    """
    candidates = [d for d in docs if d["score"] >= min_score]
    if not candidates:
        return []

    selected: list[dict] = []

    while len(selected) < top_k and candidates:
        best_idx = -1
        best_mmr = float("-inf")

        for i, doc in enumerate(candidates):
            relevance = doc["score"]

            # Penalise similarity to already-selected docs
            if selected:
                max_sim = max(simple_overlap(doc["text"], s["text"]) for s in selected)
            else:
                max_sim = 0.0

            mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = i

        if best_idx >= 0:
            selected.append(candidates.pop(best_idx))

    return selected


def rag_with_mmr(query: str, docs: list[dict]) -> str:
    selected = mmr_select(docs, top_k=4)

    if not selected:
        return "No relevant documents found."

    context = "\n\n---\n\n".join(d["text"] for d in selected)
    print(f"MMR selected {len(selected)}/{len(docs)} docs")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


docs = [
    {"text": "SMTP config: host=smtp.example.com port=587 TLS required.", "score": 0.91},
    {"text": "SMTP setup: use port 587 with STARTTLS. Host: smtp.example.com.", "score": 0.88},  # Near-duplicate
    {"text": "Password reset emails are sent when user clicks 'Forgot password'.", "score": 0.73},
    {"text": "Email templates are stored in /templates/email/.", "score": 0.61},
    {"text": "Database user table: id, email, reset_token, token_expiry.", "score": 0.48},
]

# MMR will pick doc-1 (best SMTP), doc-3 (diverse: reset flow), doc-4 (diverse: templates)
# and skip doc-2 (near-duplicate of doc-1)
print(rag_with_mmr("How do I configure SMTP for password reset emails?", docs))

# Expected Token Savings: removes redundant near-duplicate docs; better coverage per token
# Environment: RAG over large corpora where multiple similar documents are common (wikis, docs sites)
```

---

### Option 5 — Adaptive K based on query specificity

Use fewer documents for specific queries (which need precise info) and more for vague queries (which need broad coverage).

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def estimate_query_specificity(query: str) -> float:
    """
    Estimate how specific the query is (0 = vague, 1 = highly specific).
    Specific queries have proper nouns, numbers, exact terms.
    """
    words = query.split()
    specific_indicators = sum(1 for w in words if (
        w[0].isupper() or  # Proper noun
        any(c.isdigit() for c in w) or  # Contains number
        len(w) > 12 or  # Long technical term
        w in ("configure", "install", "error", "exception", "port", "endpoint")
    ))
    return min(1.0, specific_indicators / max(len(words), 1))


def adaptive_k(query: str, max_k: int = 8, min_k: int = 2) -> int:
    specificity = estimate_query_specificity(query)
    # Specific queries: fewer docs (high precision needed)
    # Vague queries: more docs (broad coverage needed)
    k = max_k - int((max_k - min_k) * specificity)
    return max(min_k, k)


def rag_adaptive(query: str, docs: list[dict]) -> str:
    k = adaptive_k(query)
    min_score = 0.60 if estimate_query_specificity(query) > 0.5 else 0.50

    selected = sorted(
        [d for d in docs if d["score"] >= min_score],
        key=lambda d: d["score"],
        reverse=True,
    )[:k]

    print(f"Query specificity: {estimate_query_specificity(query):.2f} → K={k}")

    context = "\n\n---\n\n".join(d["text"] for d in selected) if selected else "[No results]"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


docs = [{"text": f"Doc {i} content about various topics", "score": 0.9 - i*0.1} for i in range(8)]

print(rag_adaptive("Configure SMTP port 587 TLS authentication", docs))  # Specific → K=2
print(rag_adaptive("How does the system work", docs))  # Vague → K=8

# Expected Token Savings: specific queries use 2-3 docs instead of 8; saves 1500–2500 tokens
# Environment: conversational RAG agents handling both specific and exploratory queries
```

---

### Option 6 — Two-stage retrieval: coarse filter then LLM relevance judge

Retrieve broadly (top-20), then use Haiku to judge each doc's relevance in a single batched call.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def batch_relevance_judge(query: str, docs: list[dict]) -> list[dict]:
    """
    Ask Haiku to judge relevance of up to 10 docs in one call.
    Returns docs with added 'relevant' boolean.
    """
    if not docs:
        return []

    doc_list = "\n".join(
        f"{i}: {d['text'][:150]}..."
        for i, d in enumerate(docs[:10])
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Query: {query}

For each document below, return 1 if it would help answer the query, 0 if not.
Return a JSON array of 0/1 values, one per document.

Documents:
{doc_list}

Relevance array (JSON):"""
        }]
    )

    raw = response.content[0].text.strip()
    try:
        judgments = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: keep all docs
        return docs

    return [
        {**doc, "llm_relevant": bool(judgments[i])}
        for i, doc in enumerate(docs[:len(judgments)])
    ]


def rag_two_stage(query: str, all_docs: list[dict]) -> str:
    # Stage 1: coarse filter by embedding score
    coarse = sorted(all_docs, key=lambda d: d["score"], reverse=True)[:10]

    # Stage 2: LLM relevance judge (one Haiku call for all 10)
    judged = batch_relevance_judge(query, coarse)
    relevant = [d for d in judged if d.get("llm_relevant", True)][:5]

    print(f"Two-stage: {len(all_docs)} → {len(coarse)} (embedding) → {len(relevant)} (LLM judge)")

    if not relevant:
        return "No relevant documents found."

    context = "\n\n---\n\n".join(d["text"] for d in relevant)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


docs = [
    {"text": "SMTP setup uses port 587 with STARTTLS...", "score": 0.91},
    {"text": "Password reset API endpoint POST /auth/forgot-password...", "score": 0.73},
    {"text": "Database migration guide for v3 to v4...", "score": 0.65},
    {"text": "Pricing plans: starter, professional, enterprise...", "score": 0.58},
    {"text": "Email template variables: {reset_link}, {user_name}...", "score": 0.71},
]

print(rag_two_stage("How do I configure SMTP for password reset emails?", docs))

# Expected Token Savings: one Haiku call (~80 tokens) eliminates 2-4 irrelevant Sonnet-context docs
#   (~400-800 tokens each) → net savings 320–2400 tokens per query
# Environment: production RAG where retrieval quality is critical and doc corpus is noisy
```

---

## Comparison

| Option | Filter Method | Reduces Redundancy | LLM Cost | Handles Large K | Best For |
|--------|--------------|-------------------|----------|-----------------|----------|
| 1 | Score threshold | No | None | Yes | Simple, low-cost filter |
| 2 | Cross-encoder rerank | No | Haiku/doc | Partial | High-precision RAG |
| 3 | Token budget | No | None | Yes | Context-overflow prevention |
| 4 | MMR diversity | Yes | None | Yes | Wikis, docs sites with duplicates |
| 5 | Adaptive K | No | None | Yes | Mixed specific/vague queries |
| 6 | Two-stage LLM judge | Partial | One Haiku call | Yes | Noisy retrieval corpora |

**Recommended starting point:** Option 1 (score threshold ≥ 0.65) for any RAG pipeline — add 3 lines of filtering before context injection. Tune the threshold on your specific corpus. Add Option 4 (MMR) when your document corpus has many near-duplicate entries. Use Option 6 (two-stage) when retrieval precision is critical and the cost of one Haiku call per query is acceptable.
