---
layout: solution
title: "Agent Doesn't Implement Context-Aware Document Ranking Before Injection"
category: context-window
description: "Agent injects all retrieved documents into context in arbitrary order — wasting tokens on low-relevance passages and burying the most important content where the model's attention is weakest."
tags: [context-window, rag, document-ranking, reranking, relevance, retrieval]
---

# Agent Doesn't Implement Context-Aware Document Ranking Before Injection

## Problem

After retrieval, most agents inject documents as-is: whatever the vector store returned, in whatever order. This causes:

- **Lost-in-the-middle effect**: models perform worst on content in the middle of a long context; important documents buried in the middle are effectively ignored
- **Token waste**: low-relevance documents consume context budget that could hold better content
- **Answer degradation**: the model's final answer is dominated by whichever document happened to be first, regardless of actual relevance
- **Irrelevant injection**: retrieved documents that are topically related but not actually helpful still consume context

**Root cause:** No re-ranking or relevance scoring step between retrieval and injection. Documents go straight from the vector store into the prompt.

---

## Option 1: LLM-Based Relevance Scoring Before Injection

Score each retrieved document against the query using a cheap model; inject only top-K.

```python
import anthropic
import json

client = anthropic.Anthropic()

RELEVANCE_SCORER_SYSTEM = """Rate how relevant this document is for answering the given question.

Score from 0 to 10:
- 10: Directly answers the question
- 7-9: Highly relevant, contains key information
- 4-6: Somewhat relevant, tangentially useful
- 1-3: Loosely related, unlikely to help
- 0: Not relevant at all

Reply with ONLY: SCORE:<0-10> REASON:<one sentence>"""

def score_document_relevance(query: str, document: str) -> tuple[float, str]:
    """Score a document's relevance to a query."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        system=RELEVANCE_SCORER_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Question: {query}\n\nDocument:\n{document[:400]}"
        }]
    )
    text = response.content[0].text.strip()
    import re
    m = re.search(r'SCORE:?\s*(\d+)', text)
    score = int(m.group(1)) / 10.0 if m else 0.5
    reason_m = re.search(r'REASON:?\s*(.+)', text)
    reason = reason_m.group(1).strip() if reason_m else ""
    return score, reason

def rank_and_filter_documents(
    query: str,
    documents: list[dict],
    top_k: int = 3,
    min_score: float = 0.5
) -> list[dict]:
    """Score, rank, and filter documents by relevance."""
    scored = []
    for doc in documents:
        score, reason = score_document_relevance(query, doc["content"])
        scored.append({**doc, "relevance_score": score, "relevance_reason": reason})
        print(f"[rank] Score {score:.1f}: {doc['title'][:40]} — {reason[:50]}")

    # Filter by minimum score and take top-K
    filtered = [d for d in scored if d["relevance_score"] >= min_score]
    ranked = sorted(filtered, key=lambda d: d["relevance_score"], reverse=True)[:top_k]
    print(f"[rank] {len(documents)} docs → {len(ranked)} after ranking (min_score={min_score})")
    return ranked

def build_ranked_context(documents: list[dict]) -> str:
    """Build context with most relevant docs first."""
    parts = []
    for i, doc in enumerate(documents, 1):
        parts.append(
            f"[Document {i} — Relevance: {doc['relevance_score']:.1f}]\n"
            f"Title: {doc['title']}\n{doc['content']}"
        )
    return "\n\n---\n\n".join(parts)

# Simulated retrieved documents (as if from a vector store)
RETRIEVED_DOCS = [
    {"title": "Python GIL Overview", "content": "The Global Interpreter Lock (GIL) prevents multiple threads from executing Python bytecode simultaneously. This affects CPU-bound multi-threaded programs."},
    {"title": "Python Web Scraping", "content": "Use requests and BeautifulSoup for web scraping. Respect robots.txt and implement rate limiting."},
    {"title": "Asyncio vs Threading", "content": "For I/O-bound tasks, asyncio is more efficient than threading. For CPU-bound tasks, use multiprocessing to bypass the GIL."},
    {"title": "Python Package Management", "content": "Use pip and virtual environments. Consider Poetry for dependency management."},
    {"title": "Multiprocessing in Python", "content": "The multiprocessing module creates separate processes, each with its own GIL. Use Pool for parallel CPU-bound work."},
]

QUERY = "How do I speed up CPU-intensive Python code using parallelism?"

def run_agent_with_ranked_context(query: str, raw_docs: list[dict]) -> str:
    # Rank documents before injection
    ranked_docs = rank_and_filter_documents(query, raw_docs, top_k=3, min_score=0.4)
    context = build_ranked_context(ranked_docs)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f"Answer the question using the provided documents. Cite document numbers.\n\n{context}",
        messages=[{"role": "user", "content": query}]
    )
    return response.content[0].text

result = run_agent_with_ranked_context(QUERY, RETRIEVED_DOCS)
print(f"\nAnswer:\n{result}")

# Expected Token Savings: ~40% (inject 3 of 5 docs; irrelevant ones eliminated)
# Environment: RAG agents, knowledge base Q&A, document search assistants
```

---

## Option 2: Position-Aware Injection — Place Best Documents at Boundaries

Exploit the "lost-in-the-middle" effect by placing the most relevant documents at the start and end of context.

```python
import anthropic
import math

client = anthropic.Anthropic()

def cosine_sim_fake(text_a: str, text_b: str) -> float:
    """Fake cosine similarity based on word overlap (replace with real embeddings)."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    intersection = words_a & words_b
    denom = math.sqrt(len(words_a) * len(words_b))
    return len(intersection) / denom if denom > 0 else 0.0

def rank_by_relevance(query: str, documents: list[dict]) -> list[dict]:
    scored = [(cosine_sim_fake(query, d["content"]), d) for d in documents]
    return [doc for _, doc in sorted(scored, reverse=True)]

def position_aware_injection(ranked_docs: list[dict]) -> list[dict]:
    """
    Arrange documents to exploit boundary attention:
    - Most relevant → position 0 (start)
    - Second most relevant → last position (end)
    - Least relevant → middle
    This counters the "lost-in-the-middle" degradation.
    """
    if len(ranked_docs) <= 2:
        return ranked_docs

    # Split into top-2 and the rest
    top = ranked_docs[:2]
    middle = ranked_docs[2:]

    # Place best at start, second best at end, rest in middle
    return [top[0]] + middle + [top[1]]

def build_context_string(docs: list[dict], query: str) -> str:
    parts = [f"Query: {query}\n\nRelevant documents:"]
    for i, doc in enumerate(docs, 1):
        parts.append(f"\n[Doc {i}] {doc['title']}\n{doc['content']}")
    return "\n".join(parts)

DOCS = [
    {"title": "Docker Networking", "content": "Docker containers communicate via bridge networks. Use docker network create for custom networks."},
    {"title": "Kubernetes Services", "content": "Kubernetes Services expose pods via ClusterIP, NodePort, or LoadBalancer. Use selectors to route traffic to pods."},
    {"title": "Kubernetes Ingress", "content": "Ingress controllers route external HTTP/HTTPS traffic to Services. Supports host-based and path-based routing rules."},
    {"title": "Docker Volumes", "content": "Docker volumes persist data outside containers. Use named volumes for production data persistence."},
    {"title": "Container DNS", "content": "Kubernetes CoreDNS resolves service names to ClusterIP addresses. Services are reachable at <service>.<namespace>.svc.cluster.local."},
]

QUERY = "How does traffic routing work in Kubernetes for external HTTP requests?"

def run_position_aware_agent(query: str, docs: list[dict]) -> str:
    # Step 1: Rank by relevance
    ranked = rank_by_relevance(query, docs)
    print(f"[position] Ranked order: {[d['title'][:20] for d in ranked]}")

    # Step 2: Rearrange for position-aware attention
    positioned = position_aware_injection(ranked)
    print(f"[position] After position-aware arrangement: {[d['title'][:20] for d in positioned]}")

    context = build_context_string(positioned, query)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": context + "\n\nAnswer the query based on the documents above."}]
    )
    return response.content[0].text

result = run_position_aware_agent(QUERY, DOCS)
print(f"\nAnswer:\n{result[:300]}...")

# Expected Token Savings: ~0% (same docs, better arrangement; improves answer quality not token count)
# Environment: RAG systems with 5+ retrieved documents; knowledge-intensive Q&A where answer accuracy matters
```

---

## Option 3: Cross-Encoder Re-ranking with Passage-Level Scoring

Score relevance at the passage level (not document level) to inject only the most useful paragraphs.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def split_into_passages(document: dict, max_chars: int = 300) -> list[dict]:
    """Split a document into paragraph-sized passages."""
    content = document["content"]
    paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
    if not paragraphs:
        paragraphs = [content[i:i+max_chars] for i in range(0, len(content), max_chars)]

    return [
        {
            "doc_title": document["title"],
            "passage_id": f"{document['title'][:10]}_{i}",
            "content": para,
            "doc_metadata": document.get("metadata", {})
        }
        for i, para in enumerate(paragraphs)
    ]

def batch_score_passages(query: str, passages: list[dict]) -> list[dict]:
    """Score multiple passages in one LLM call."""
    passage_list = "\n\n".join(
        f"[{i+1}] {p['content'][:200]}" for i, p in enumerate(passages)
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"""Rate each passage's relevance to the question on a scale of 0-10.
Question: {query}

Passages:
{passage_list}

Reply with JSON array of scores only: [score1, score2, ...]"""
        }]
    )

    try:
        scores = json.loads(response.content[0].text)
        if not isinstance(scores, list):
            scores = [5] * len(passages)
    except json.JSONDecodeError:
        # Fallback: extract numbers
        nums = re.findall(r'\d+', response.content[0].text)
        scores = [int(n) for n in nums[:len(passages)]] + [5] * len(passages)
        scores = scores[:len(passages)]

    return [
        {**p, "relevance": s / 10.0}
        for p, s in zip(passages, scores)
    ]

def passage_level_rank_and_inject(
    query: str,
    documents: list[dict],
    top_k_passages: int = 5
) -> str:
    # Split all documents into passages
    all_passages = []
    for doc in documents:
        all_passages.extend(split_into_passages(doc))

    print(f"[passage] {len(documents)} docs → {len(all_passages)} passages")

    # Score all passages (batch them to reduce API calls)
    batch_size = 5
    scored_passages = []
    for i in range(0, len(all_passages), batch_size):
        batch = all_passages[i:i + batch_size]
        scored_passages.extend(batch_score_passages(query, batch))

    # Take top-K passages sorted by relevance
    top_passages = sorted(scored_passages, key=lambda p: p["relevance"], reverse=True)[:top_k_passages]

    context_parts = []
    for p in top_passages:
        context_parts.append(
            f"[From '{p['doc_title']}' — Relevance: {p['relevance']:.1f}]\n{p['content']}"
        )
        print(f"[passage] {p['relevance']:.1f}: {p['content'][:60]}...")

    return "\n\n---\n\n".join(context_parts)

LONG_DOCS = [
    {
        "title": "Kubernetes Architecture Guide",
        "content": """The control plane manages the cluster state.\n\nThe API server validates and processes REST operations.\n\nEtcd stores all cluster data as key-value pairs.\n\nThe scheduler assigns pods to nodes based on resource requirements.\n\nKubectl is the command-line tool for cluster management."""
    },
    {
        "title": "Kubernetes Networking",
        "content": """Every pod gets its own IP address.\n\nServices provide stable network endpoints for pods.\n\nIngresscontrollers handle external HTTP traffic routing.\n\nNetwork policies control pod-to-pod communication.\n\nCoreDNS provides service discovery within the cluster."""
    },
]

QUERY = "How does the Kubernetes scheduler decide where to place pods?"

context = passage_level_rank_and_inject(QUERY, LONG_DOCS, top_k_passages=3)
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=300,
    messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {QUERY}"}]
)
print(f"\nAnswer:\n{response.content[0].text}")

# Expected Token Savings: ~60% (passage-level top-5 from 10 paragraphs; only inject the essential excerpts)
# Environment: Long document RAG; technical documentation Q&A; legal and medical document search
```

---

## Option 4: MMR (Maximal Marginal Relevance) — Balance Relevance and Diversity

Avoid injecting near-duplicate passages that cover the same information; maximize relevance AND diversity.

```python
import anthropic
import math

client = anthropic.Anthropic()

def word_overlap_sim(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    return len(wa & wb) / max(len(wa | wb), 1)

def mmr_select(
    query: str,
    documents: list[dict],
    top_k: int = 4,
    lambda_param: float = 0.6  # 0=max diversity, 1=max relevance
) -> list[dict]:
    """Select documents using Maximal Marginal Relevance."""
    if not documents:
        return []

    # Score relevance to query
    for doc in documents:
        doc["query_sim"] = word_overlap_sim(query, doc["content"])

    selected = []
    remaining = list(documents)

    while len(selected) < top_k and remaining:
        best = None
        best_score = -float("inf")

        for doc in remaining:
            # Relevance to query
            rel_score = doc["query_sim"]

            # Similarity to already-selected documents (redundancy penalty)
            if selected:
                max_sim = max(word_overlap_sim(doc["content"], s["content"]) for s in selected)
            else:
                max_sim = 0.0

            # MMR score: balance relevance and novelty
            mmr_score = lambda_param * rel_score - (1 - lambda_param) * max_sim

            if mmr_score > best_score:
                best_score = mmr_score
                best = doc

        if best:
            best["mmr_score"] = best_score
            selected.append(best)
            remaining.remove(best)
            print(f"[mmr] Selected: {best['title'][:40]} "
                  f"(rel={best['query_sim']:.2f}, mmr={best_score:.2f})")

    return selected

DOCS = [
    {"title": "Python asyncio basics", "content": "asyncio provides cooperative multitasking using async/await syntax for I/O bound tasks"},
    {"title": "Python async intro", "content": "async def and await keywords enable non-blocking I/O in Python using asyncio event loop"},
    {"title": "asyncio event loop", "content": "The asyncio event loop runs coroutines and manages I/O callbacks without threads"},
    {"title": "Python threading", "content": "threading module provides OS-level threads; GIL limits parallel CPU execution"},
    {"title": "aiohttp library", "content": "aiohttp enables async HTTP client/server operations using asyncio for web requests"},
    {"title": "asyncio vs trio", "content": "trio offers structured concurrency as an alternative to asyncio with clearer cancellation semantics"},
]

QUERY = "How does Python handle concurrent I/O operations?"

def run_mmr_agent(query: str, docs: list[dict]) -> str:
    # MMR selection: balanced relevance + diversity
    selected = mmr_select(query, docs, top_k=3, lambda_param=0.6)
    context = "\n\n".join(f"[{d['title']}]\n{d['content']}" for d in selected)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text

result = run_mmr_agent(QUERY, DOCS)
print(f"\nAnswer:\n{result}")

# Expected Token Savings: ~35% (MMR eliminates near-duplicate content; 3 diverse docs vs 6 redundant)
# Environment: Knowledge bases with overlapping content; wiki search; any RAG with high document redundancy
```

---

## Option 5: Query-Adaptive Context Budget — Allocate More Space to Harder Queries

Estimate query complexity and allocate proportionally more context budget to harder questions.

```python
import anthropic
import re

client = anthropic.Anthropic()

def estimate_query_complexity(query: str) -> float:
    """Estimate 0-1 complexity score from query features."""
    q = query.lower()
    score = 0.3  # baseline

    # Multi-part questions
    score += min(q.count("?") * 0.1, 0.2)
    score += min(q.count(" and ") * 0.05, 0.15)
    score += min(q.count("compare") * 0.1, 0.1)

    # Specific technical indicators
    if any(w in q for w in ["why", "how does", "explain", "analyze"]):
        score += 0.2
    if any(w in q for w in ["what is", "define", "list"]):
        score -= 0.1

    return max(0.1, min(1.0, score))

def allocate_context_budget(
    query: str,
    documents: list[dict],
    total_budget_chars: int = 4000
) -> list[dict]:
    """Allocate more context to more complex queries."""
    complexity = estimate_query_complexity(query)
    print(f"[budget] Query complexity: {complexity:.2f}")

    # Scale: more complex = include more documents and longer excerpts
    max_docs = max(2, min(len(documents), int(2 + complexity * 4)))
    chars_per_doc = total_budget_chars // max_docs

    print(f"[budget] Allocating {max_docs} docs, {chars_per_doc} chars each")

    # Score and select top docs
    scored = []
    for doc in documents:
        overlap = len(set(query.lower().split()) & set(doc["content"].lower().split()))
        scored.append((overlap, doc))
    scored.sort(reverse=True)

    selected = []
    for _, doc in scored[:max_docs]:
        excerpt = doc["content"][:chars_per_doc]
        selected.append({**doc, "content": excerpt, "truncated": len(doc["content"]) > chars_per_doc})

    return selected

def run_budget_adaptive_agent(query: str, documents: list[dict]) -> str:
    selected_docs = allocate_context_budget(query, documents, total_budget_chars=3000)

    context_parts = [
        f"[Doc {i+1}: {d['title']}{'...' if d.get('truncated') else ''}]\n{d['content']}"
        for i, d in enumerate(selected_docs)
    ]
    context = "\n\n".join(context_parts)
    print(f"[budget] Context: {len(selected_docs)} docs, {len(context)} chars")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"Documents:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text

DOCS = [{"title": f"Doc {i}", "content": f"Content about topic {i} with details " * 30} for i in range(6)]

# Simple query → less context
print("=== Simple query ===")
run_budget_adaptive_agent("What is topic 1?", DOCS)

# Complex query → more context
print("\n=== Complex query ===")
run_budget_adaptive_agent(
    "Compare and explain how topics 1, 2, and 3 relate to each other and why their differences matter",
    DOCS
)

# Expected Token Savings: ~30% on simple queries (fewer docs, shorter excerpts for simple lookups)
# Environment: General-purpose RAG agents handling both simple lookups and complex analytical questions
```

---

## Option 6: Hybrid Ranking — Combine Lexical, Semantic, and Recency Signals

Rank documents using a weighted combination of BM25-style lexical match, semantic similarity, and recency.

```python
import anthropic
import math
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class RankedDocument:
    title: str
    content: str
    created_at: float = field(default_factory=time.time)
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    recency_score: float = 0.0
    final_score: float = 0.0

def bm25_score(query: str, doc_content: str, k1: float = 1.5, b: float = 0.75) -> float:
    """Simplified BM25 scoring."""
    query_terms = query.lower().split()
    doc_terms = doc_content.lower().split()
    doc_len = len(doc_terms)
    avg_doc_len = 100  # Assume average
    score = 0.0
    term_freq = {}
    for t in doc_terms:
        term_freq[t] = term_freq.get(t, 0) + 1
    for term in query_terms:
        tf = term_freq.get(term, 0)
        idf = math.log(1 + 1 / (tf + 1))  # Simplified
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * doc_len / avg_doc_len)
        score += idf * numerator / denominator
    return score

def fake_semantic_score(query: str, doc_content: str) -> float:
    """Word overlap as semantic proxy (replace with real embeddings)."""
    qw = set(query.lower().split())
    dw = set(doc_content.lower().split())
    return len(qw & dw) / math.sqrt(max(len(qw) * len(dw), 1))

def recency_score(created_at: float, half_life_days: float = 30) -> float:
    """Exponential decay based on document age."""
    age_days = (time.time() - created_at) / 86400
    return math.exp(-age_days / half_life_days)

def hybrid_rank(
    query: str,
    documents: list[dict],
    weights: dict = None,
    top_k: int = 4
) -> list[RankedDocument]:
    if weights is None:
        weights = {"lexical": 0.3, "semantic": 0.5, "recency": 0.2}

    ranked = []
    for doc in documents:
        rd = RankedDocument(
            title=doc["title"],
            content=doc["content"],
            created_at=doc.get("created_at", time.time() - doc.get("age_days", 0) * 86400)
        )
        rd.lexical_score = bm25_score(query, doc["content"])
        rd.semantic_score = fake_semantic_score(query, doc["content"])
        rd.recency_score = recency_score(rd.created_at)

        # Normalize and combine
        rd.final_score = (
            weights["lexical"] * rd.lexical_score +
            weights["semantic"] * rd.semantic_score +
            weights["recency"] * rd.recency_score
        )
        ranked.append(rd)
        print(f"[hybrid] {doc['title'][:30]:30} lex={rd.lexical_score:.2f} "
              f"sem={rd.semantic_score:.2f} rec={rd.recency_score:.2f} "
              f"→ final={rd.final_score:.2f}")

    return sorted(ranked, key=lambda d: d.final_score, reverse=True)[:top_k]

DOCS_WITH_DATES = [
    {"title": "Python 3.12 async improvements", "content": "asyncio task groups and improved error propagation in Python 3.12", "age_days": 5},
    {"title": "Python async tutorial 2019", "content": "Introduction to async/await syntax for asynchronous programming in Python", "age_days": 1800},
    {"title": "Trio structured concurrency", "content": "trio provides structured concurrency patterns as an asyncio alternative for async tasks", "age_days": 200},
    {"title": "asyncio performance tips", "content": "Optimizing asyncio event loop performance with uvloop and connection pooling", "age_days": 90},
    {"title": "Python threading guide", "content": "Threading module usage for parallel execution with GIL limitations", "age_days": 400},
]

QUERY = "modern async Python performance"

def run_hybrid_ranked_agent(query: str, docs: list[dict]) -> str:
    top_docs = hybrid_rank(query, docs, top_k=3)
    context = "\n\n".join(
        f"[{d.title} — Score: {d.final_score:.2f}]\n{d.content}"
        for d in top_docs
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text

result = run_hybrid_ranked_agent(QUERY, DOCS_WITH_DATES)
print(f"\nAnswer:\n{result[:300]}...")

# Expected Token Savings: ~45% (top-3 of 5 docs; recency weighting drops stale content automatically)
# Environment: Documentation search with version drift; news/blog RAG where recency matters; enterprise knowledge bases
```

---

## Comparison

| Option | Ranking Signal | Passage-Level | Diversity | Recency | Best For |
|--------|---------------|---------------|-----------|---------|----------|
| 1. LLM Relevance Scoring | LLM judgment | No | No | No | General RAG with diverse domains |
| 2. Position-Aware Injection | Word overlap | No | No | No | Long contexts; accuracy-critical Q&A |
| 3. Passage-Level Scoring | LLM batch scoring | Yes | No | No | Long documents with mixed relevance |
| 4. MMR Selection | Overlap + diversity | No | Yes | No | Redundant document collections |
| 5. Query-Adaptive Budget | Complexity estimation | No | No | No | Mixed simple/complex query traffic |
| 6. Hybrid Ranking | BM25 + semantic + recency | No | No | Yes | News, docs with version drift |
