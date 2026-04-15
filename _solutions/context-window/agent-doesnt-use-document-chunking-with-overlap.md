---
layout: solution
title: "Agent Doesn't Use Document Chunking with Overlap"
category: context-window
description: "Agent splits documents into hard chunks at fixed character boundaries, losing context at every split point. Key sentences that span chunk boundaries are severed, causing retrieval to miss relevant passages and the model to produce incomplete answers."
tags: [context-window, rag, chunking, retrieval, embeddings]
---

## Symptom

The agent's RAG pipeline returns answers that are cut off or miss context that exists in the document:

```
Document: "...The treatment showed 78% efficacy in the Phase 3 trial.
           However, patients with renal impairment should use a reduced dose of 5mg
           rather than the standard 10mg dose. Contraindications include..."

Chunk boundary at character 1000:
  Chunk 1 ends: "...showed 78% efficacy in the Phase 3 trial."
  Chunk 2 starts: "However, patients with renal impairment..."

Query: "What is the efficacy of the treatment?"
Retrieved: Chunk 1 only → answer: "78% efficacy" (misses the dose caveat)

Query: "What dose for renal impairment?"
Retrieved: Chunk 2 only → answer: "5mg" (misses the 78% context for risk-benefit)
```

Each chunk is missing the sentence that came just before or after it.

## Root Cause

Hard chunking at fixed boundaries without overlap:

```python
def chunk_document(text: str, chunk_size: int = 1000) -> list[str]:
    # Simple fixed-size split — severs sentences at boundaries
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
```

This approach treats text as a byte array, not as a semantic unit. Sentences, paragraphs, and logical sections that span chunk boundaries are split with no context on either side.

---

## Fix

### Option 1 — Fixed-size chunking with character overlap

Add an overlap window so each chunk repeats the tail of the previous chunk. The key sentence at the boundary appears in both adjacent chunks.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def chunk_with_overlap(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 200,
) -> list[dict]:
    """Split text into chunks with overlap. Each chunk records its position."""
    chunks = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk_text = text[start:end]

        chunks.append({
            "text": chunk_text,
            "start": start,
            "end": end,
            "chunk_index": len(chunks),
        })

        if end == len(text):
            break

        # Next chunk starts overlap characters before this chunk ends
        start = end - overlap

    return chunks


def rag_query(document: str, query: str) -> str:
    chunks = chunk_with_overlap(document, chunk_size=800, overlap=150)

    # Simple keyword retrieval — replace with embeddings in production
    query_words = set(query.lower().split())
    scored = [
        (sum(1 for w in query_words if w in c["text"].lower()), c)
        for c in chunks
    ]
    scored.sort(key=lambda x: -x[0])
    top_chunks = [c["text"] for _, c in scored[:3] if _[0] > 0]

    if not top_chunks:
        return "No relevant content found."

    context = "\n\n---\n\n".join(top_chunks)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"Answer based on context only:\n\n{context}\n\nQuestion: {query}"
        }]
    )
    return response.content[0].text.strip()


doc = """The treatment showed 78% efficacy in the Phase 3 trial (n=2,400).
However, patients with renal impairment should use a reduced dose of 5mg
rather than the standard 10mg dose. Contraindications include severe hepatic failure."""

print(rag_query(doc, "What dose for renal impairment patients?"))

# Expected Token Savings: overlap increases chunk count ~20% but eliminates missed-boundary answers
# Environment: RAG pipelines processing long documents (reports, contracts, manuals)
```

---

### Option 2 — Sentence-boundary chunking with semantic overlap

Split at sentence boundaries instead of arbitrary character positions. Sentences are never severed.

```python
import re
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def split_sentences(text: str) -> list[str]:
    """Split text at sentence boundaries."""
    # Match end of sentence: period/!/? followed by space+capital or end of string
    pattern = r'(?<=[.!?])\s+(?=[A-Z])'
    sentences = re.split(pattern, text.strip())
    return [s.strip() for s in sentences if s.strip()]


def chunk_by_sentences(
    text: str,
    max_chars: int = 800,
    overlap_sentences: int = 2,
) -> list[dict]:
    sentences = split_sentences(text)
    chunks = []
    current: list[str] = []
    current_len = 0

    for i, sent in enumerate(sentences):
        if current_len + len(sent) > max_chars and current:
            # Save current chunk
            chunks.append({
                "text": " ".join(current),
                "sentence_start": i - len(current),
                "sentence_end": i,
                "chunk_index": len(chunks),
            })
            # Start next chunk with overlap: last N sentences of current chunk
            current = current[-overlap_sentences:] if overlap_sentences else []
            current_len = sum(len(s) for s in current)

        current.append(sent)
        current_len += len(sent)

    if current:
        chunks.append({
            "text": " ".join(current),
            "sentence_start": len(sentences) - len(current),
            "sentence_end": len(sentences),
            "chunk_index": len(chunks),
        })

    return chunks


def rag_query_sentences(document: str, query: str) -> str:
    chunks = chunk_by_sentences(document, max_chars=600, overlap_sentences=2)

    # Score chunks by keyword overlap
    query_words = set(query.lower().split())
    scored = sorted(
        [(sum(1 for w in query_words if w in c["text"].lower()), c) for c in chunks],
        key=lambda x: -x[0],
    )
    top = [c["text"] for score, c in scored[:3] if score > 0]
    context = "\n\n".join(top) if top else "No relevant chunks found."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


doc = ("The compound achieved statistical significance (p=0.003) across all endpoints. "
       "However, the subgroup analysis revealed diminished effect in patients over 75. "
       "Dose adjustment is required for this population. The standard dose is 10mg daily.")
print(rag_query_sentences(doc, "What dose for elderly patients?"))

# Expected Token Savings: no truncated sentences → fewer follow-up clarification calls
# Environment: medical, legal, or scientific documents where sentence integrity is critical
```

---

### Option 3 — Paragraph-aware chunking with context header

Split at paragraph boundaries. Prepend a context header (document title + section heading) to every chunk so retrieval and the model always know where the chunk came from.

```python
import re
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def chunk_by_paragraphs(
    text: str,
    max_chars: int = 800,
    context_header: str = "",
) -> list[dict]:
    """Split at double-newline paragraph boundaries."""
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    chunks = []
    current_paras: list[str] = []
    current_len = len(context_header)

    for para in paragraphs:
        if current_len + len(para) > max_chars and current_paras:
            chunk_text = context_header + "\n\n" + "\n\n".join(current_paras) if context_header else "\n\n".join(current_paras)
            chunks.append({"text": chunk_text, "chunk_index": len(chunks)})
            # Keep last paragraph as overlap
            current_paras = [current_paras[-1]]
            current_len = len(context_header) + len(current_paras[0])

        current_paras.append(para)
        current_len += len(para)

    if current_paras:
        chunk_text = context_header + "\n\n" + "\n\n".join(current_paras) if context_header else "\n\n".join(current_paras)
        chunks.append({"text": chunk_text, "chunk_index": len(chunks)})

    return chunks


def rag_with_header(document: str, title: str, query: str) -> str:
    header = f"[Document: {title}]"
    chunks = chunk_by_paragraphs(document, max_chars=700, context_header=header)

    query_words = set(query.lower().split())
    scored = sorted(
        [(sum(1 for w in query_words if w in c["text"].lower()), c) for c in chunks],
        key=lambda x: -x[0],
    )
    top = [c["text"] for score, c in scored[:2] if score > 0]
    context = "\n\n---\n\n".join(top) if top else "No relevant sections found."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


doc = """Introduction

This report covers the Q1 2026 financial results for Acme Corp.

Revenue Performance

Total revenue reached $124M, up 18% year-over-year. The growth was driven primarily
by the enterprise segment, which expanded 34% to $67M.

Cost Structure

Operating expenses increased 12% to $89M due to headcount expansion in engineering."""

print(rag_with_header(doc, "Acme Q1 2026 Report", "What drove revenue growth?"))

# Expected Token Savings: context header eliminates "which document?" follow-up questions
# Environment: multi-document RAG where the model needs to attribute answers to sources
```

---

### Option 4 — Semantic chunking using the model to detect topic boundaries

Use Haiku to identify topic-change boundaries in the document. Chunk at semantic boundaries rather than arbitrary positions.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def find_semantic_boundaries(text: str, window: int = 5) -> list[int]:
    """Ask Haiku to identify paragraph indices where the topic changes."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if len(paragraphs) <= window:
        return []  # Too short to need chunking

    # Ask model to identify topic shifts
    para_list = "\n".join(f"{i}: {p[:100]}..." for i, p in enumerate(paragraphs))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Identify paragraph indices where the topic significantly changes.
Return JSON array of integers (0-based indices).

Paragraphs:
{para_list}

Boundary indices (JSON array):"""
        }]
    )

    raw = response.content[0].text.strip()
    try:
        boundaries = json.loads(raw)
        return [b for b in boundaries if isinstance(b, int) and 0 < b < len(paragraphs)]
    except (json.JSONDecodeError, TypeError):
        return []


def semantic_chunks(text: str) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    boundaries = find_semantic_boundaries(text)
    boundaries = sorted(set([0] + boundaries + [len(paragraphs)]))

    chunks = []
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        chunk = "\n\n".join(paragraphs[start:end])
        if chunk:
            chunks.append(chunk)

    return chunks


def rag_semantic(document: str, query: str) -> str:
    chunks = semantic_chunks(document)

    query_words = set(query.lower().split())
    scored = sorted(
        [(sum(1 for w in query_words if w in c.lower()), c) for c in chunks],
        key=lambda x: -x[0],
    )
    context = "\n\n---\n\n".join(c for _, c in scored[:2] if _ > 0) or "No match."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


doc = """The history of computing begins with mechanical calculators in the 17th century.
Charles Babbage designed the Difference Engine in 1822.

In a completely different domain, quantum mechanics emerged from experiments in the early 20th century.
Planck's constant was proposed in 1900 to explain blackbody radiation.

Returning to computing, the transistor was invented at Bell Labs in 1947.
This enabled the modern era of digital computing."""

print(semantic_chunks(doc))

# Expected Token Savings: coherent topic chunks retrieve better → fewer re-queries
# Environment: diverse documents (books, reports) where topic sections aren't explicitly marked
```

---

### Option 5 — Hierarchical chunking: parent and child chunks

Store both large parent chunks (for full context) and small child chunks (for precise retrieval). Retrieve by child, return parent for answering.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class Chunk:
    text: str
    chunk_id: str
    parent_id: str | None
    level: int  # 0=parent, 1=child


def hierarchical_chunks(
    text: str,
    parent_size: int = 1200,
    child_size: int = 300,
    overlap: int = 50,
) -> list[Chunk]:
    chunks: list[Chunk] = []

    # Level 0: parent chunks
    p_start = 0
    p_idx = 0
    while p_start < len(text):
        p_end = min(p_start + parent_size, len(text))
        parent_text = text[p_start:p_end]
        parent_id = f"p{p_idx}"

        chunks.append(Chunk(
            text=parent_text,
            chunk_id=parent_id,
            parent_id=None,
            level=0,
        ))

        # Level 1: child chunks within this parent
        c_start = 0
        c_idx = 0
        while c_start < len(parent_text):
            c_end = min(c_start + child_size, len(parent_text))
            child_text = parent_text[c_start:c_end]
            chunks.append(Chunk(
                text=child_text,
                chunk_id=f"{parent_id}_c{c_idx}",
                parent_id=parent_id,
                level=1,
            ))
            if c_end == len(parent_text):
                break
            c_start = c_end - overlap
            c_idx += 1

        if p_end == len(text):
            break
        p_start = p_end - overlap
        p_idx += 1

    return chunks


def rag_hierarchical(document: str, query: str) -> str:
    all_chunks = hierarchical_chunks(document)

    children = [c for c in all_chunks if c.level == 1]
    parents  = {c.chunk_id: c for c in all_chunks if c.level == 0}

    # Retrieve by child (precise)
    query_words = set(query.lower().split())
    scored = sorted(
        [(sum(1 for w in query_words if w in c.text.lower()), c) for c in children],
        key=lambda x: -x[0],
    )

    # Return parent (full context)
    seen_parents = set()
    context_chunks = []
    for score, child in scored[:3]:
        if score > 0 and child.parent_id not in seen_parents:
            parent = parents.get(child.parent_id)
            if parent:
                context_chunks.append(parent.text)
                seen_parents.add(child.parent_id)

    context = "\n\n---\n\n".join(context_chunks) if context_chunks else "No context."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


doc = " ".join([f"Sentence {i} about topic {i//5}." for i in range(50)])
print(rag_hierarchical(doc, "topic 2"))

# Expected Token Savings: child chunks give precise retrieval signal; parent gives full context
# Environment: production RAG where precision retrieval AND full context are both required
```

---

### Option 6 — Sliding window chunking with deduplication

Use a sliding window so every position in the document belongs to multiple overlapping chunks. At retrieval time, deduplicate overlapping results.

```python
import anthropic
import hashlib

client = anthropic.Anthropic(api_key="sk-live-...")


def sliding_window_chunks(
    text: str,
    window_size: int = 600,
    step: int = 200,
) -> list[dict]:
    """Dense overlapping chunks: every point in document covered by 3 chunks."""
    chunks = []
    start = 0

    while start < len(text):
        end = min(start + window_size, len(text))
        chunk_text = text[start:end]
        chunk_hash = hashlib.md5(chunk_text.encode()).hexdigest()[:8]

        chunks.append({
            "text": chunk_text,
            "start": start,
            "end": end,
            "id": chunk_hash,
        })

        if end == len(text):
            break
        start += step

    return chunks


def rag_sliding(document: str, query: str, top_k: int = 3) -> str:
    chunks = sliding_window_chunks(document, window_size=500, step=150)

    query_words = set(query.lower().split())
    scored = sorted(
        [(sum(1 for w in query_words if w in c["text"].lower()), c) for c in chunks],
        key=lambda x: -x[0],
    )

    # Deduplicate: skip chunks that overlap heavily with already-selected chunks
    selected: list[dict] = []
    selected_ranges: list[tuple[int, int]] = []

    for score, chunk in scored:
        if score == 0:
            break
        if len(selected) >= top_k:
            break

        # Check overlap with already-selected chunks
        c_start, c_end = chunk["start"], chunk["end"]
        overlap_pct = max(
            (min(c_end, sel_end) - max(c_start, sel_start)) / (c_end - c_start)
            for sel_start, sel_end in selected_ranges
        ) if selected_ranges else 0.0

        if overlap_pct < 0.5:  # Less than 50% overlap — include
            selected.append(chunk)
            selected_ranges.append((c_start, c_end))

    context = "\n\n".join(c["text"] for c in selected) if selected else "No match."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
    )
    return response.content[0].text.strip()


doc = ("The company was founded in 2015 by Alice Chen and Bob Smith. "
       "Alice previously worked at Google on search algorithms. "
       "Bob brings experience from his time at Goldman Sachs in structured finance. "
       "Together they raised $12M Series A in 2017 from Sequoia Capital. "
       "The flagship product launched in 2018 and reached profitability in 2020.")

print(rag_sliding(doc, "Who founded the company?"))

# Expected Token Savings: deduplication prevents sending the same passage 3 times to the model
# Environment: short documents where maximum recall matters (legal clauses, spec sheets)
```

---

## Comparison

| Option | Boundary Quality | Overlap Type | Context Preservation | Dedup | Complexity |
|--------|-----------------|--------------|----------------------|-------|------------|
| 1 | Fixed chars | Character overlap | Good | No | Low |
| 2 | Sentence | Sentence overlap | Excellent | No | Low |
| 3 | Paragraph | Last paragraph | Good + header | No | Low |
| 4 | Semantic (model) | None (natural) | Best | No | Medium |
| 5 | Hierarchical | Parent covers child | Excellent | Implicit | Medium |
| 6 | Sliding window | Dense | Maximum | Yes | Medium |

**Recommended starting point:** Option 2 (sentence boundaries + 2-sentence overlap) for most RAG pipelines — zero model calls at chunk time, no truncated sentences, simple to implement. Use Option 5 (hierarchical) when you need both high retrieval precision and full surrounding context for the model.
