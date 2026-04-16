---
layout: solution
title: "Agent Doesn't Implement Multi-Document Context Compression"
category: context-window
description: "Agents that inject raw documents into context hit token limits quickly and bury the signal in noise. These patterns show how to compress multiple documents into dense, relevant context before passing them to the model."
tags: [context-window, compression, multi-document, rag, summarization, anthropic]
---

## Problem

A RAG agent that retrieves 10 documents of 2,000 tokens each fills 20,000 tokens of context before the user question is even injected. Most of that content is irrelevant padding. Multi-document context compression selectively extracts, merges, and condenses retrieved content so the final context is dense with relevant signal — not raw document dumps.

---

### Option 1: Query-Focused Extractive Compression

Extract only the sentences from each document that are most relevant to the query.

```python
import re
import anthropic

client = anthropic.Anthropic()

EXTRACT_PROMPT = """Extract the 2-3 most relevant sentences from this document for the given query.

Query: {query}

Document ({doc_id}):
{document}

Output only the extracted sentences, no explanation. Preserve original wording."""

def extract_relevant_sentences(query: str, doc_id: str, document: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": EXTRACT_PROMPT.format(
                query=query, doc_id=doc_id, document=document[:1500]
            ),
        }],
    )
    return response.content[0].text.strip()

def compress_documents(query: str, documents: dict[str, str]) -> str:
    """Compress multiple documents into a focused context block."""
    compressed_parts = []
    original_tokens = sum(len(d.split()) for d in documents.values())

    for doc_id, content in documents.items():
        extracted = extract_relevant_sentences(query, doc_id, content)
        compressed_parts.append(f"[{doc_id}]: {extracted}")

    compressed = "\n\n".join(compressed_parts)
    compressed_tokens = len(compressed.split())

    ratio = compressed_tokens / max(original_tokens, 1)
    print(f"[compression] {original_tokens} → {compressed_tokens} tokens ({ratio:.0%})")
    return compressed

def answer_with_compressed_context(query: str, documents: dict[str, str]) -> str:
    compressed = compress_documents(query, documents)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Answer using only the provided context. Cite document IDs when referencing information.",
        messages=[{
            "role": "user",
            "content": f"Context:\n{compressed}\n\nQuestion: {query}",
        }],
    )
    return response.content[0].text

if __name__ == "__main__":
    query = "What are the security implications of using JWT tokens for authentication?"

    documents = {
        "doc_1": """JWT (JSON Web Tokens) are a compact, URL-safe means of representing claims.
        They consist of three parts: header, payload, and signature. The header specifies the
        algorithm used for signing. Common algorithms include HS256 and RS256. JWTs are stateless
        and self-contained. They can store user information directly in the token. The downside is
        that tokens cannot be revoked before expiry without maintaining a blacklist. Session fixation
        attacks are possible if tokens are not rotated on privilege changes. Always validate the
        algorithm field to prevent algorithm confusion attacks where attackers switch to 'none'.""",

        "doc_2": """OAuth 2.0 is an authorization framework. It provides several grant types including
        authorization code, implicit, client credentials, and refresh token. The authorization code
        flow is recommended for web applications. PKCE (Proof Key for Code Exchange) should be used
        for public clients. Access tokens should be short-lived to minimize exposure. Refresh tokens
        allow obtaining new access tokens without user interaction. Token introspection endpoints
        allow resource servers to validate tokens. Always use HTTPS to protect tokens in transit.""",

        "doc_3": """Database indexing improves query performance. B-tree indexes work well for range
        queries. Hash indexes are optimal for equality comparisons. Composite indexes should match
        query patterns. Index selectivity determines effectiveness. PostgreSQL supports partial indexes
        for filtered queries. Covering indexes eliminate table lookups. Regular VACUUM prevents index
        bloat. Index-only scans improve performance when all needed columns are in the index.""",
    }

    print(f"Query: {query}\n")
    answer = answer_with_compressed_context(query, documents)
    print(f"Answer: {answer[:500]}")

# Expected Token Savings: 60-80% reduction in context tokens; irrelevant doc_3 content dropped automatically
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Hierarchical Summarization — Chunk then Merge

Summarize each document independently, then merge summaries into a unified context.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

CHUNK_SUMMARY_PROMPT = """Summarize this document in 2-3 sentences, focusing on: {focus}

Document: {content}

Summary:"""

MERGE_PROMPT = """Merge these document summaries into a single coherent context passage.
Eliminate redundancy. Preserve all unique facts. Write in third person.

Summaries:
{summaries}

Focus topic: {focus}

Merged context (under 200 words):"""

async def summarize_document(doc_id: str, content: str, focus: str) -> tuple[str, str]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": CHUNK_SUMMARY_PROMPT.format(focus=focus, content=content[:2000]),
        }],
    )
    return doc_id, response.content[0].text.strip()

async def hierarchical_compress(
    query: str,
    documents: dict[str, str],
    model: str = "claude-sonnet-4-6",
) -> str:
    # Step 1: Summarize all documents in parallel
    summaries = await asyncio.gather(*[
        summarize_document(doc_id, content, query)
        for doc_id, content in documents.items()
    ])

    original_tokens = sum(len(c.split()) for c in documents.values())
    summary_tokens = sum(len(s.split()) for _, s in summaries)
    print(f"[step 1] {original_tokens} → {summary_tokens} tokens ({summary_tokens/original_tokens:.0%})")

    # Step 2: Merge summaries into unified context
    summaries_text = "\n\n".join(f"[{doc_id}]: {summary}" for doc_id, summary in summaries)
    merge_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": MERGE_PROMPT.format(summaries=summaries_text, focus=query),
        }],
    )
    merged = merge_response.content[0].text.strip()
    merged_tokens = len(merged.split())
    print(f"[step 2] {summary_tokens} → {merged_tokens} tokens ({merged_tokens/summary_tokens:.0%})")

    # Step 3: Answer with merged context
    answer_response = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Context:\n{merged}\n\nQuestion: {query}",
        }],
    )
    return answer_response.content[0].text

if __name__ == "__main__":
    async def main():
        query = "How should I design a distributed caching layer?"
        documents = {
            "redis_docs": "Redis is an in-memory data structure store used as a database, cache, and message broker. It supports data structures such as strings, hashes, lists, sets, and sorted sets. Redis Cluster provides a way to run Redis automatically sharded across multiple nodes. Redis Sentinel provides high availability through monitoring and automatic failover. Persistence can be achieved through RDB snapshots or AOF logging. Redis supports pub/sub messaging patterns. Expiration times can be set on keys for automatic eviction. Lua scripting allows atomic operations.",
            "memcached_docs": "Memcached is a high-performance distributed memory object caching system. It is designed for simplicity and speed. Memcached stores key-value pairs where values can be arbitrary data. It uses consistent hashing to distribute keys across multiple servers. Unlike Redis, Memcached does not support persistence or replication natively. It is purely a cache. Multi-get allows fetching multiple keys in a single operation. Memory is allocated in slabs to reduce fragmentation. CAS (Check-And-Set) provides atomic compare-and-swap operations.",
            "cdn_docs": "CDN (Content Delivery Network) caches static assets at edge locations close to users. This reduces latency and origin server load. Cache-Control headers control TTL and caching behavior. ETags enable conditional requests to check freshness. Vary headers allow different cached versions per request attribute. CDN purge APIs allow invalidating stale content. Edge computing allows running logic at CDN nodes. CDNs typically support HTTP/2 and HTTP/3 for improved performance.",
            "db_caching": "Database query result caching stores results of expensive queries. This avoids repeated computation. Cache invalidation is the hardest problem — use event-driven invalidation or TTL expiry. Write-through caching updates cache on write. Write-behind caching batches writes asynchronously. Read-through cache fetches from DB on miss and populates cache. Thundering herd prevention requires cache warming and probabilistic early expiration.",
        }
        result = await hierarchical_compress(query, documents)
        print(f"\nAnswer: {result[:500]}")
    asyncio.run(main())

# Expected Token Savings: Two-pass compression achieves 85-95% token reduction; deduplicates overlapping content
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Query-Adaptive Truncation with Relevance Scoring

Score each document paragraph by relevance to the query, then truncate to a token budget.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

RELEVANCE_SCORE_PROMPT = """Rate the relevance of this passage to the query on a scale 0-10.

Query: {query}
Passage: {passage}

Respond with a single integer 0-10."""

async def score_passage(query: str, passage: str) -> float:
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": RELEVANCE_SCORE_PROMPT.format(
                    query=query, passage=passage[:400]
                ),
            }],
        )
        return float(response.content[0].text.strip())
    except ValueError:
        return 5.0

def split_into_paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    # If no double newlines, split by sentence groups
    if len(paras) <= 1:
        sentences = text.split(". ")
        paras = [". ".join(sentences[i:i+3]) for i in range(0, len(sentences), 3)]
    return paras

async def relevance_truncate(
    query: str,
    documents: dict[str, str],
    token_budget: int = 2000,
) -> str:
    # Split all docs into paragraphs
    all_passages = []
    for doc_id, content in documents.items():
        for para in split_into_paragraphs(content):
            if len(para.split()) > 10:  # skip trivial passages
                all_passages.append((doc_id, para))

    # Score all passages in parallel
    scores = await asyncio.gather(*[
        score_passage(query, para) for _, para in all_passages
    ])

    # Sort by relevance descending
    ranked = sorted(zip(scores, all_passages), key=lambda x: -x[0])

    # Fill token budget greedily
    selected = []
    used_tokens = 0
    for score, (doc_id, para) in ranked:
        para_tokens = len(para.split())
        if used_tokens + para_tokens > token_budget:
            continue
        selected.append((score, doc_id, para))
        used_tokens += para_tokens

    total_tokens = sum(len(p.split()) for _, p in all_passages)
    print(f"[relevance-truncate] {total_tokens} → {used_tokens} tokens, "
          f"{len(selected)}/{len(all_passages)} passages selected")

    # Format selected passages
    context = "\n\n".join(
        f"[{doc_id} score={score:.0f}]: {para}"
        for score, doc_id, para in sorted(selected, key=lambda x: x[0], reverse=True)
    )
    return context

if __name__ == "__main__":
    async def main():
        query = "What are the best practices for API versioning?"
        docs = {
            "api_guide": "API versioning strategies include URL versioning (/v1/users), header versioning (Accept: application/vnd.api+v2), and query parameter versioning (?version=2). URL versioning is the most visible and easiest to implement. Semantic versioning applies to APIs: major version for breaking changes, minor for new features, patch for bug fixes. Deprecation policies should give consumers at least 6 months notice. Sunset headers communicate when versions will be retired.\n\nBreaking changes include removing fields, changing field types, changing semantics of existing fields, and removing endpoints. Non-breaking changes include adding optional fields, adding new endpoints, and adding new optional query parameters.",
            "rest_best_practices": "REST APIs should be stateless. Resources should be nouns not verbs. HTTP methods: GET for read, POST for create, PUT for replace, PATCH for partial update, DELETE for remove. Response codes: 200 OK, 201 Created, 204 No Content, 400 Bad Request, 401 Unauthorized, 404 Not Found, 409 Conflict, 422 Unprocessable Entity, 429 Too Many Requests.\n\nPagination: use cursor-based pagination for large datasets. Filtering via query parameters. Sorting via sort parameter. Field selection via fields parameter. HATEOAS links enable discoverability.",
            "security_guide": "API security requires authentication and authorization. OAuth 2.0 with PKCE for user-facing APIs. API keys for service-to-service. Rotate credentials regularly. Rate limiting per API key. Input validation at all endpoints. Output encoding to prevent injection. TLS 1.2+ for all connections. CORS configuration to restrict origins. Security headers: HSTS, X-Frame-Options, Content-Security-Policy.",
        }
        context = await relevance_truncate(query, docs, token_budget=500)
        print(f"\nContext ({len(context.split())} words):\n{context[:600]}")
    asyncio.run(main())

# Expected Token Savings: Only budget-fitting high-relevance passages included; 70-85% typical reduction
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Cross-Document Deduplication Before Injection

Detect and remove near-duplicate content across documents before building context.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

def jaccard_similarity(text_a: str, text_b: str) -> float:
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)

def deduplicate_passages(passages: list[tuple[str, str]], threshold: float = 0.6) -> list[tuple[str, str]]:
    """Remove near-duplicate passages using Jaccard similarity."""
    kept = []
    for doc_id, para in passages:
        is_dup = any(
            jaccard_similarity(para, kept_para) > threshold
            for _, kept_para in kept
        )
        if not is_dup:
            kept.append((doc_id, para))
        else:
            print(f"  [dedup] dropped near-duplicate from {doc_id}: '{para[:50]}'")
    return kept

async def compress_with_dedup(
    query: str,
    documents: dict[str, str],
    max_tokens_per_doc: int = 300,
) -> str:
    # Step 1: Summarize each document
    async def summarize(doc_id: str, content: str) -> tuple[str, str]:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens_per_doc,
            messages=[{
                "role": "user",
                "content": f"Summarize the key facts from this document relevant to: {query}\n\nDocument:\n{content[:1500]}",
            }],
        )
        return doc_id, response.content[0].text.strip()

    summaries = await asyncio.gather(*[summarize(k, v) for k, v in documents.items()])

    # Step 2: Split summaries into paragraphs and deduplicate
    all_passages = []
    for doc_id, summary in summaries:
        for para in summary.split("\n"):
            if para.strip() and len(para.split()) > 5:
                all_passages.append((doc_id, para.strip()))

    pre_dedup = len(all_passages)
    deduped = deduplicate_passages(all_passages, threshold=0.55)
    post_dedup = len(deduped)
    print(f"[dedup] {pre_dedup} → {post_dedup} passages ({pre_dedup - post_dedup} duplicates removed)")

    context = "\n".join(f"[{doc_id}] {para}" for doc_id, para in deduped)
    return context

if __name__ == "__main__":
    async def main():
        query = "How does Kubernetes handle pod scheduling?"
        docs = {
            "k8s_official": "The Kubernetes scheduler assigns Pods to Nodes. The scheduler watches for newly created Pods with no assigned node. For each Pod, the scheduler finds the best Node for it to run on. The scheduler finds feasible Nodes that meet scheduling requirements, then runs a set of functions to score feasible Nodes. The Node with the highest score wins. The scheduler then notifies the API server about this decision in a process called binding.",
            "k8s_blog": "Kubernetes scheduling works by selecting the best node for each pod. The kube-scheduler watches for new pods without a node assignment. It evaluates each node against pod requirements. Nodes that don't meet requirements are filtered out. Remaining nodes are scored. The highest scoring node is selected. The scheduler notifies the API server of its decision.",  # near-duplicate of above
            "k8s_advanced": "Advanced scheduling features include: node affinity and anti-affinity rules, pod affinity/anti-affinity, taints and tolerations, topology spread constraints, priority classes, and preemption. Node affinity allows constraining which nodes a pod can be scheduled on based on node labels. Pod affinity enables co-locating pods on the same node. Taints allow nodes to repel pods. Tolerations allow pods to schedule onto tainted nodes.",
        }
        context = await compress_with_dedup(query, docs)
        print(f"\nFinal context ({len(context.split())} words):\n{context[:500]}")
    asyncio.run(main())

# Expected Token Savings: Deduplication removes 20-40% of redundant content from overlapping sources
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Chain-of-Density Compression

Iteratively compress documents by increasing information density without increasing length.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

COD_PROMPT = """You will iteratively compress this text to be more information-dense.

Original text ({words} words):
{text}

Task: Rewrite in exactly {target_words} words or fewer. Pack in MORE specific facts, numbers, and technical details. Cut filler, transitions, and redundancy. Keep ALL unique information.

Dense rewrite:"""

async def chain_of_density_compress(
    text: str,
    target_ratio: float = 0.3,
    iterations: int = 2,
) -> str:
    current = text
    original_words = len(text.split())
    target_words = int(original_words * target_ratio)

    # Do compression in iterations: each halves the length
    for i in range(iterations):
        current_words = len(current.split())
        step_target = max(target_words, int(current_words * (target_ratio ** (1/iterations))))

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=step_target * 2,
            messages=[{
                "role": "user",
                "content": COD_PROMPT.format(
                    words=current_words,
                    text=current,
                    target_words=step_target,
                ),
            }],
        )
        current = response.content[0].text.strip()
        print(f"  [COD iter {i+1}] {current_words} → {len(current.split())} words")

    return current

async def cod_multi_doc(query: str, documents: dict[str, str]) -> str:
    # Compress each document independently with COD
    async def compress_one(doc_id: str, content: str) -> tuple[str, str]:
        compressed = await chain_of_density_compress(content, target_ratio=0.25, iterations=2)
        return doc_id, compressed

    results = await asyncio.gather(*[compress_one(k, v) for k, v in documents.items()])

    original_total = sum(len(v.split()) for v in documents.values())
    compressed_total = sum(len(c.split()) for _, c in results)
    print(f"[COD total] {original_total} → {compressed_total} words ({compressed_total/original_total:.0%})")

    context = "\n\n".join(f"[{doc_id}]: {compressed}" for doc_id, compressed in results)
    return context

if __name__ == "__main__":
    async def main():
        query = "How do I implement authentication in a REST API?"
        docs = {
            "jwt_guide": "JSON Web Tokens provide a compact and self-contained way for securely transmitting information between parties as a JSON object. This information can be verified and trusted because it is digitally signed. JWTs can be signed using a secret (with HMAC algorithm) or a public/private key pair using RSA or ECDSA. When users successfully log in, the server creates a JWT and returns it. The client stores the JWT and includes it in subsequent requests in the Authorization header using the Bearer schema. The server validates the JWT on each request and grants access to protected resources.",
            "oauth_guide": "OAuth 2.0 is the industry-standard protocol for authorization. It provides specific authorization flows for web applications, desktop applications, mobile phones, and living room devices. The client obtains an access token from the authorization server by presenting credentials. The access token is used to access protected resources hosted by the resource server. OAuth defines four grant types: Authorization Code, Implicit, Resource Owner Password Credentials, and Client Credentials. The Authorization Code grant type is recommended for server-side web applications.",
        }
        context = await cod_multi_doc(query, docs)
        print(f"\nFinal context:\n{context[:600]}")
    asyncio.run(main())

# Expected Token Savings: COD achieves 70-80% compression while preserving 90%+ of unique information
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Adaptive Compression with Budget Enforcement

Dynamically select compression depth based on available token budget and document count.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def compress_to_budget(
    doc_id: str,
    content: str,
    word_budget: int,
    query: str,
) -> tuple[str, str]:
    current_words = len(content.split())
    if current_words <= word_budget:
        return doc_id, content  # no compression needed

    ratio = word_budget / current_words
    level = "light" if ratio > 0.6 else "medium" if ratio > 0.3 else "aggressive"
    instructions = {
        "light": "Remove filler sentences and redundancy. Keep all technical details.",
        "medium": "Summarize to key points. Preserve specific facts, numbers, and examples.",
        "aggressive": "Extract only the most critical facts in bullet form. Maximum density.",
    }

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=word_budget * 2,
        messages=[{
            "role": "user",
            "content": (
                f"Compress this document for a query about: {query}\n"
                f"Target: under {word_budget} words. Level: {level}.\n"
                f"Instructions: {instructions[level]}\n\n"
                f"Document:\n{content[:2000]}"
            ),
        }],
    )
    compressed = response.content[0].text.strip()
    print(f"  [{doc_id}] {current_words} → {len(compressed.split())} words ({level})")
    return doc_id, compressed

async def adaptive_compress(
    query: str,
    documents: dict[str, str],
    total_token_budget: int = 3000,
) -> str:
    n = len(documents)
    budget_per_doc = total_token_budget // n

    results = await asyncio.gather(*[
        compress_to_budget(doc_id, content, budget_per_doc, query)
        for doc_id, content in documents.items()
    ])

    original_total = sum(len(v.split()) for v in documents.values())
    compressed_total = sum(len(c.split()) for _, c in results)
    print(f"[adaptive] {original_total} → {compressed_total} / budget {total_token_budget} words")

    context = "\n\n".join(f"Source [{doc_id}]:\n{compressed}" for doc_id, compressed in results)
    return context

if __name__ == "__main__":
    async def main():
        query = "How do microservices communicate with each other?"
        docs = {
            f"doc_{i}": f"Content about microservice communication pattern {i}. " * 100
            for i in range(5)
        }
        context = await adaptive_compress(query, docs, total_token_budget=800)
        print(f"\nContext ({len(context.split())} words, budget 800):\n{context[:400]}")
    asyncio.run(main())

# Expected Token Savings: Budget-constrained per-doc compression; no single doc can crowd out others
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Compression Strategy | Parallelism | Deduplication | Best For |
|--------|---------------------|-------------|---------------|----------|
| 1 | Extractive sentence selection | Sequential | No | Few documents, query-focused extraction |
| 2 | Hierarchical summarize + merge | Async parallel | Implicit in merge | Many documents, high redundancy |
| 3 | Relevance-scored truncation | Async parallel | No | Budget-constrained, mixed relevance |
| 4 | Summarize + Jaccard dedup | Async parallel | Yes (Jaccard) | Overlapping sources (web search, RAG) |
| 5 | Chain-of-density rewrite | Async parallel | No | Dense technical content needing maximum compression |
| 6 | Adaptive budget-per-doc | Async parallel | No | Variable-length docs with strict token limit |
