---
layout: solution
title: "Agent Doesn't Implement Multi-Document Context Fusion"
category: context-window
description: "Intelligently merge content from multiple source documents into a coherent, deduplicated context block instead of naively concatenating all documents and overflowing the context window."
tags: [context-window, documents, fusion, deduplication, rag, retrieval]
---

When agents receive multiple documents — search results, database records, uploaded files — they often concatenate them verbatim. This duplicates shared content (headers, boilerplate, repeated facts), buries the relevant signal in noise, and frequently overflows the context window. Multi-document fusion merges sources intelligently: extracting relevant sections, deduplicating overlapping content, and presenting a single coherent context block instead of a stack of full documents.

## Option 1: Relevance-Filtered Excerpt Extraction

Score each document against the query, extract only the most relevant paragraphs from each, and concatenate the excerpts with source attribution. Irrelevant documents are excluded; relevant ones contribute only their key passages.

```python
import anthropic
import re
from dataclasses import dataclass

@dataclass
class Document:
    source: str
    content: str

def split_paragraphs(text: str) -> list[str]:
    paras = re.split(r"\n{2,}", text.strip())
    return [p.strip() for p in paras if len(p.strip()) > 50]

def word_overlap(query: str, text: str) -> float:
    q_words = set(re.findall(r"\w+", query.lower()))
    t_words = set(re.findall(r"\w+", text.lower()))
    if not q_words:
        return 0.0
    return len(q_words & t_words) / len(q_words)

def extract_relevant_excerpts(
    doc: Document,
    query: str,
    top_k: int = 3,
    min_score: float = 0.15,
) -> list[tuple[str, float]]:
    paras = split_paragraphs(doc.content)
    scored = [(p, word_overlap(query, p)) for p in paras]
    filtered = [(p, s) for p, s in scored if s >= min_score]
    return sorted(filtered, key=lambda x: x[1], reverse=True)[:top_k]

def fuse_documents(
    documents: list[Document],
    query: str,
    max_chars: int = 6000,
) -> str:
    sections = []
    total_chars = 0

    for doc in documents:
        excerpts = extract_relevant_excerpts(doc, query)
        if not excerpts:
            continue
        header = f"### Source: {doc.source}"
        body = "\n\n".join(p for p, _ in excerpts)
        section = f"{header}\n{body}"
        if total_chars + len(section) > max_chars:
            remaining = max_chars - total_chars
            section = section[:remaining] + "... [truncated]"
            sections.append(section)
            break
        sections.append(section)
        total_chars += len(section)

    return "\n\n---\n\n".join(sections) if sections else "No relevant content found."

def answer_from_documents(documents: list[Document], query: str) -> str:
    client = anthropic.Anthropic()
    fused = fuse_documents(documents, query)

    orig_chars = sum(len(d.content) for d in documents)
    fused_chars = len(fused)
    print(f"[Fusion] {orig_chars:,} → {fused_chars:,} chars ({(1-fused_chars/orig_chars)*100:.1f}% reduction)")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Context from {len(documents)} documents:\n\n{fused}\n\nQuestion: {query}",
        }],
    )
    return response.content[0].text

if __name__ == "__main__":
    docs = [
        Document("python_docs", """
Python is an interpreted, high-level, general-purpose programming language.
Created by Guido van Rossum and first released in 1991.

Python's design philosophy emphasizes code readability and simplicity.
The language provides constructs that enable clear programming on both small and large scales.

Python supports multiple programming paradigms including procedural, object-oriented, and functional programming.
Python uses dynamic typing and garbage collection.

Python's standard library is comprehensive and includes modules for network protocols, file operations, and more.
The Python Package Index (PyPI) hosts hundreds of thousands of third-party modules.
        """),
        Document("async_guide", """
asyncio is a library to write concurrent code using the async/await syntax.
asyncio is used as a foundation for multiple Python asynchronous frameworks.

Coroutines are the fundamental building blocks of async Python.
They are defined using async def and can be suspended with await.

Event loops manage the execution of coroutines and I/O operations.
The event loop runs in a single thread, enabling cooperative multitasking.

Python's asyncio was first introduced in Python 3.4 and has evolved significantly.
Python's design philosophy emphasizes code readability and simplicity.
        """),
        Document("web_frameworks", """
Django is a high-level Python web framework that encourages rapid development.
Django follows the model-view-template (MVT) architectural pattern.

Flask is a lightweight WSGI web application framework.
Flask is designed to make getting started quick and easy.

FastAPI is a modern, fast web framework for building APIs with Python 3.8+.
FastAPI is based on standard Python type hints and is one of the fastest frameworks available.

Python's design philosophy emphasizes code readability and simplicity.
All these frameworks benefit from Python's extensive standard library.
        """),
    ]

    result = answer_from_documents(docs, "How does Python handle async programming?")
    print(result)

# Expected Token Savings: 40-70% reduction from eliminating boilerplate and off-topic sections
# Environment: pip install anthropic
```

## Option 2: LLM-Driven Fusion Summarization

Send all documents to a cheap model and ask it to produce a single fused summary that answers the query, resolving contradictions and merging overlapping information. The summary replaces all individual documents as the context for the main model call.

```python
import anthropic
from dataclasses import dataclass

@dataclass
class Document:
    source: str
    content: str
    metadata: dict = None

FUSION_SYSTEM = """You are a document fusion engine. Given multiple source documents and a query,
produce a single comprehensive, non-redundant summary that:
1. Answers the query directly with all relevant information
2. Removes repeated facts that appear in multiple sources
3. Notes any contradictions between sources
4. Preserves specific data (numbers, dates, names) with their source
5. Uses concise, dense prose — no headers, no bullet points
Output ONLY the fused summary."""

def fuse_with_llm(
    documents: list[Document],
    query: str,
    max_doc_chars: int = 3000,
) -> tuple[str, int]:
    client = anthropic.Anthropic()

    # Truncate individual docs to prevent overflowing fusion model context
    doc_blocks = []
    for doc in documents:
        content = doc.content[:max_doc_chars]
        if len(doc.content) > max_doc_chars:
            content += f"\n[...truncated {len(doc.content)-max_doc_chars} chars]"
        doc_blocks.append(f"[SOURCE: {doc.source}]\n{content}")

    combined = "\n\n---\n\n".join(doc_blocks)
    fusion_prompt = f"Query: {query}\n\nDocuments:\n{combined}\n\nProduce a fused summary:"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        system=FUSION_SYSTEM,
        messages=[{"role": "user", "content": fusion_prompt}],
    )
    fused = response.content[0].text
    fusion_tokens = response.usage.input_tokens + response.usage.output_tokens
    return fused, fusion_tokens

def answer_with_fused_context(documents: list[Document], query: str) -> str:
    client = anthropic.Anthropic()
    fused_summary, fusion_cost = fuse_with_llm(documents, query)

    print(f"[LLM-Fusion] Fusion used {fusion_cost} tokens, produced {len(fused_summary.split())} word summary")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Using this fused summary of {len(documents)} documents:\n\n{fused_summary}\n\nAnswer: {query}",
        }],
    )
    return response.content[0].text

if __name__ == "__main__":
    docs = [
        Document("report_q1", "Q1 revenue was $2.4M, up 12% YoY. Top product: Widget Pro at $800K. Customer count: 1,240. Churn rate: 3.2%. New contracts: 47. Average deal size: $51K."),
        Document("report_q2", "Q2 revenue reached $2.8M, a 17% increase. Widget Pro remained top seller at $920K. Customer count grew to 1,380. Churn improved to 2.9%. New contracts: 53."),
        Document("analyst_notes", "Widget Pro dominates revenue. Customer retention improving each quarter. Q1 churn 3.2%, Q2 churn 2.9% — positive trend. Revenue growth accelerating: Q1 +12%, Q2 +17%. Recommend expanding Widget Pro line."),
    ]
    result = answer_with_fused_context(docs, "What are the key revenue trends and what should we prioritize?")
    print(result)

# Expected Token Savings: 50-65% on main model call; fusion model cost is 5-10x lower than main model
# Environment: pip install anthropic
```

## Option 3: Deduplication-First Chunked Fusion

Break each document into chunks, compute a fingerprint for each chunk, and deduplicate across all documents before building the context. Near-duplicate chunks (boilerplate, repeated headers) are stored once and marked with all their sources.

```python
import anthropic
import hashlib
import re
from dataclasses import dataclass, field

@dataclass
class Chunk:
    text: str
    source: str
    fingerprint: str
    score: float = 0.0

def fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]

def near_duplicate(a: str, b: str, threshold: float = 0.8) -> bool:
    """Jaccard similarity on trigrams."""
    def trigrams(s):
        words = s.lower().split()
        return set(zip(words, words[1:], words[2:])) if len(words) >= 3 else set()
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return a.strip() == b.strip()
    return len(ta & tb) / len(ta | tb) >= threshold

def chunk_and_deduplicate(
    documents: list[tuple[str, str]],  # (source, content)
    chunk_size: int = 200,
    query: str = "",
) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    seen_fingerprints: dict[str, Chunk] = {}

    for source, content in documents:
        words = content.split()
        for i in range(0, len(words), chunk_size):
            text = " ".join(words[i:i + chunk_size])
            fp = fingerprint(text)

            if fp in seen_fingerprints:
                seen_fingerprints[fp].source += f", {source}"
                continue

            # Near-duplicate check against recent chunks
            is_near_dup = False
            for existing in list(seen_fingerprints.values())[-20:]:
                if near_duplicate(text, existing.text):
                    existing.source += f", {source}"
                    is_near_dup = True
                    break

            if not is_near_dup:
                chunk = Chunk(text=text, source=source, fingerprint=fp)
                if query:
                    q_words = set(re.findall(r"\w+", query.lower()))
                    c_words = set(re.findall(r"\w+", text.lower()))
                    chunk.score = len(q_words & c_words) / len(q_words) if q_words else 0
                seen_fingerprints[fp] = chunk
                all_chunks.append(chunk)

    return sorted(all_chunks, key=lambda c: c.score, reverse=True)

def fuse_with_dedup(
    documents: list[tuple[str, str]],
    query: str,
    max_chunks: int = 15,
) -> str:
    chunks = chunk_and_deduplicate(documents, query=query)
    selected = chunks[:max_chunks]

    total_orig = sum(len(c) for _, c in documents)
    total_fused = sum(len(c.text) for c in selected)
    print(f"[DeduFusion] {len(documents)} docs → {len(chunks)} unique chunks → {len(selected)} selected ({(1-total_fused/total_orig)*100:.1f}% reduction)")

    sections = []
    for chunk in selected:
        sources = chunk.source
        sections.append(f"[{sources}] {chunk.text}")

    return "\n\n".join(sections)

def answer_with_deduped_context(documents: list[tuple[str, str]], query: str) -> str:
    client = anthropic.Anthropic()
    fused = fuse_with_dedup(documents, query)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{fused}\n\nQuestion: {query}"}],
    )
    return response.content[0].text

if __name__ == "__main__":
    shared_boilerplate = "This document is confidential and intended for internal use only. All rights reserved."
    docs = [
        ("doc_a", f"{shared_boilerplate}\n\nPython asyncio enables single-threaded concurrency via coroutines. Use async/await for I/O-bound work. The event loop schedules coroutines cooperatively."),
        ("doc_b", f"{shared_boilerplate}\n\nasyncio's event loop manages all async operations. Coroutines yield control back at await points. asyncio enables single-threaded concurrency through cooperative multitasking."),
        ("doc_c", f"{shared_boilerplate}\n\nFor CPU-bound work, use multiprocessing. asyncio is ideal for network I/O, file I/O, and other waiting operations. The GIL limits Python thread parallelism."),
    ]
    result = answer_with_deduped_context(docs, "When should I use asyncio vs multiprocessing?")
    print(result)

# Expected Token Savings: 30-60%; eliminates boilerplate and near-duplicate passages across sources
# Environment: pip install anthropic
```

## Option 4: Structured Fusion with Conflict Resolution

Parse structured documents (JSON records, tables) and merge them field by field. When fields conflict across sources, apply a resolution strategy (latest-wins, highest-confidence, explicit merge). Produces a single canonical record instead of multiple conflicting versions.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any

@dataclass
class StructuredDocument:
    source: str
    timestamp: str  # ISO 8601
    confidence: float  # 0.0-1.0
    data: dict[str, Any]

def resolve_conflict(
    field: str,
    values: list[tuple[str, Any, str, float]],  # (source, value, timestamp, confidence)
) -> tuple[Any, str]:
    """Return (resolved_value, resolution_note)."""
    if len(values) == 1:
        return values[0][1], f"single source ({values[0][0]})"

    # Strategy: highest confidence wins; on tie, latest timestamp
    best = max(values, key=lambda x: (x[3], x[2]))
    sources = ", ".join(f"{s}={v!r}" for s, v, _, _ in values)
    return best[1], f"conflict resolved: {sources} → {best[0]}"

def merge_records(documents: list[StructuredDocument]) -> tuple[dict, list[str]]:
    """Merge multiple structured documents into one canonical record."""
    all_fields: dict[str, list[tuple]] = {}
    notes: list[str] = []

    for doc in documents:
        for field, value in doc.data.items():
            all_fields.setdefault(field, []).append(
                (doc.source, value, doc.timestamp, doc.confidence)
            )

    merged = {}
    for field, candidates in all_fields.items():
        unique_values = list({str(v): (s, v, t, c) for s, v, t, c in candidates}.values())
        resolved_value, note = resolve_conflict(field, unique_values)
        merged[field] = resolved_value
        if len(unique_values) > 1:
            notes.append(f"Field '{field}': {note}")

    return merged, notes

def answer_from_structured_docs(documents: list[StructuredDocument], query: str) -> str:
    client = anthropic.Anthropic()
    merged, conflicts = merge_records(documents)

    context = f"Merged record from {len(documents)} sources:\n{json.dumps(merged, indent=2)}"
    if conflicts:
        context += f"\n\nConflicts resolved:\n" + "\n".join(f"  - {c}" for c in conflicts)

    orig_chars = sum(len(json.dumps(d.data)) for d in documents)
    print(f"[StructFusion] {len(documents)} records → 1 merged, {len(conflicts)} conflicts, {orig_chars} → {len(context)} chars")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {query}"}],
    )
    return response.content[0].text

if __name__ == "__main__":
    docs = [
        StructuredDocument("crm", "2026-04-16T10:00:00", 0.9, {
            "company": "Acme Corp",
            "revenue": 2_400_000,
            "employees": 142,
            "plan": "enterprise",
            "contact": "alice@acme.com",
        }),
        StructuredDocument("billing", "2026-04-16T14:00:00", 0.95, {
            "company": "Acme Corp",
            "revenue": 2_450_000,  # more recent
            "employees": 142,
            "plan": "enterprise_plus",  # upgraded
            "mrr": 8_500,
        }),
        StructuredDocument("support", "2026-04-15T09:00:00", 0.7, {
            "company": "Acme Corp",
            "revenue": 2_300_000,  # stale
            "open_tickets": 2,
            "nps_score": 72,
            "contact": "bob@acme.com",  # different contact
        }),
    ]
    result = answer_from_structured_docs(docs, "What is Acme Corp's current plan and revenue?")
    print(result)

# Expected Token Savings: 60-80% on structured data; one merged record vs. multiple raw records
# Environment: pip install anthropic
```

## Option 5: Async Parallel Retrieval with Fusion

Fetch multiple documents concurrently, extract relevant sections from each in parallel, then fuse into a single context. Combines retrieval parallelism with fusion compression so the total wall time is bounded by the slowest fetch, not sum of all fetches.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass

@dataclass
class AsyncDocument:
    source_id: str
    fetch_fn: object  # callable async -> str

async def fetch_and_extract(
    client: anthropic.AsyncAnthropic,
    source_id: str,
    fetch_fn,
    query: str,
    max_excerpt_tokens: int = 300,
) -> tuple[str, str]:
    """Fetch doc and extract relevant excerpt concurrently."""
    content = await fetch_fn()

    if len(content.split()) <= max_excerpt_tokens:
        return source_id, content

    # Use Haiku to extract relevant section
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_excerpt_tokens,
        messages=[{
            "role": "user",
            "content": f"Extract only the parts of this text relevant to: '{query}'\n\nText:\n{content[:4000]}",
        }],
    )
    return source_id, response.content[0].text

async def parallel_fetch_and_fuse(
    sources: list[tuple[str, object]],  # (source_id, async_fetch_fn)
    query: str,
    max_context_chars: int = 8000,
) -> str:
    client = anthropic.AsyncAnthropic()

    tasks = [
        fetch_and_extract(client, sid, fn, query)
        for sid, fn in sources
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sections = []
    total_chars = 0
    for result in results:
        if isinstance(result, Exception):
            print(f"[ParallelFusion] Fetch error: {result}")
            continue
        source_id, excerpt = result
        section = f"[{source_id}]\n{excerpt}"
        if total_chars + len(section) > max_context_chars:
            break
        sections.append(section)
        total_chars += len(section)

    print(f"[ParallelFusion] {len(results)} sources → {len(sections)} included, {total_chars} chars")
    return "\n\n---\n\n".join(sections)

async def answer_async(sources: list[tuple[str, object]], query: str) -> str:
    client = anthropic.AsyncAnthropic()
    fused = await parallel_fetch_and_fuse(sources, query)

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Context:\n{fused}\n\nQuestion: {query}"}],
    )
    return response.content[0].text

async def main():
    # Simulate async document fetches
    async def fetch_a(): await asyncio.sleep(0.05); return "Document A: Python asyncio uses an event loop. Coroutines are defined with async def. The await keyword suspends execution until a result is ready. asyncio.gather runs coroutines concurrently."
    async def fetch_b(): await asyncio.sleep(0.03); return "Document B: Python threads are limited by the GIL for CPU work. asyncio is better for I/O-bound concurrency. Use ProcessPoolExecutor for CPU-bound parallel work."
    async def fetch_c(): await asyncio.sleep(0.07); return "Document C: The asyncio event loop is single-threaded. Multiple coroutines run cooperatively. asyncio.create_task schedules a coroutine without blocking. TaskGroups allow structured concurrency."

    sources = [("doc_a", fetch_a), ("doc_b", fetch_b), ("doc_c", fetch_c)]
    result = await answer_async(sources, "How does Python handle concurrent I/O operations?")
    print(result)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Parallel fetch adds 0ms latency; extraction compression saves 40-60% context
# Environment: pip install anthropic
```

## Option 6: Hierarchical Fusion with Citation Index

Build a two-level fusion: first fuse within document groups (by topic/source type), then fuse across groups. Maintain a citation index mapping every fact in the fused context back to its original source, enabling the model to cite sources in its answer.

```python
import anthropic
import re
from dataclasses import dataclass, field

@dataclass
class CitedFact:
    fact_id: str
    text: str
    sources: list[str]
    group: str

def extract_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]

def fuse_group(
    client: anthropic.Anthropic,
    group_name: str,
    docs: list[tuple[str, str]],
    query: str,
) -> str:
    combined = "\n".join(f"[{src}] {content}" for src, content in docs)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"Merge these documents about '{group_name}' into one paragraph, removing duplicates, relevant to: {query}\n\n{combined}",
        }],
    )
    return response.content[0].text.strip()

def hierarchical_fuse(
    document_groups: dict[str, list[tuple[str, str]]],
    query: str,
) -> tuple[str, list[CitedFact]]:
    client = anthropic.Anthropic()
    group_summaries: dict[str, str] = {}

    for group_name, docs in document_groups.items():
        summary = fuse_group(client, group_name, docs, query)
        group_summaries[group_name] = summary

    # Build citation index
    citation_index: list[CitedFact] = []
    for group_name, summary in group_summaries.items():
        sources = [src for src, _ in document_groups[group_name]]
        sentences = extract_sentences(summary)
        for i, sentence in enumerate(sentences):
            citation_index.append(CitedFact(
                fact_id=f"{group_name}_{i}",
                text=sentence,
                sources=sources,
                group=group_name,
            ))

    # Final cross-group fusion
    cross_group = "\n\n".join(
        f"## {name}\n{summary}" for name, summary in group_summaries.items()
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": f"Create a unified answer from these topic summaries for the query: {query}\n\n{cross_group}",
        }],
    )
    final_fusion = response.content[0].text
    return final_fusion, citation_index

def answer_with_citations(
    document_groups: dict[str, list[tuple[str, str]]],
    query: str,
) -> str:
    client = anthropic.Anthropic()
    fused, citations = hierarchical_fuse(document_groups, query)
    total_docs = sum(len(docs) for docs in document_groups.values())
    print(f"[HierFusion] {total_docs} docs in {len(document_groups)} groups → {len(citations)} cited facts")

    cite_map = "\n".join(f"[{c.fact_id}] {c.text} (sources: {', '.join(c.sources)})" for c in citations[:10])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Fused context:\n{fused}\n\nCitation index:\n{cite_map}\n\nAnswer with citations: {query}",
        }],
    )
    return response.content[0].text

if __name__ == "__main__":
    groups = {
        "performance": [
            ("benchmark_2024", "asyncio achieved 10k req/s on the test server. Threading reached 2k req/s due to GIL."),
            ("benchmark_2025", "asyncio performance: 12k req/s with uvloop. Multiprocessing: 8k req/s across 4 cores."),
        ],
        "use_cases": [
            ("guide_a", "Use asyncio for web scraping, API calls, database queries, and any I/O-bound work."),
            ("guide_b", "asyncio excels at handling thousands of concurrent connections with low memory overhead."),
        ],
        "limitations": [
            ("faq", "asyncio doesn't speed up CPU-bound tasks. The GIL still applies within the event loop."),
            ("blog", "Common asyncio mistake: blocking calls inside coroutines stall the entire event loop."),
        ],
    }
    result = answer_with_citations(groups, "When and how should I use asyncio for performance?")
    print(result)

# Expected Token Savings: 55-75%; hierarchical fusion produces dense, cited summaries from many docs
# Environment: pip install anthropic
```

## Comparison

| Option | Deduplication | Structured Data | Citations | Best For |
|--------|--------------|----------------|-----------|----------|
| 1. Relevance Extraction | Partial | No | Source labels | Many docs, known query |
| 2. LLM Summarization | Full | No | No | Complex narrative merging |
| 3. Chunk Deduplication | Full | No | Source per chunk | Overlapping doc corpora |
| 4. Structured Merge | N/A | Yes | Conflict notes | JSON/record-based sources |
| 5. Async Parallel | Partial | No | Source labels | Slow remote fetches |
| 6. Hierarchical + Citations | Full | No | Full index | Research-grade accuracy |
