---
layout: solution
title: "Agent Uses Full Document Embedding When Chunked Suffices"
category: token-cost
description: "Agent embeds entire documents as single vectors — a 50-page PDF becomes one embedding that loses mid-document detail. Retrieval quality degrades and embedding cost grows linearly with document size when chunked embedding would be cheaper and more accurate."
tags: [token-cost, embeddings, chunking, rag, retrieval]
---

## Symptom

An agent embeds a 40,000-token legal contract as a single vector:

```python
# Agent embeds entire document
embedding = embed(full_contract_text)  # 40,000 tokens → 1 vector
```

Two problems emerge:
1. A question about clause 47 (buried on page 32) retrieves the wrong document because the embedding represents the entire contract's average meaning, not clause 47.
2. At $0.0001/1K tokens, embedding 40K tokens costs $0.004 per document. With 10,000 contracts, that's $40 — versus $4 for 256-token chunks covering the same content with better precision.

## Root Cause

The model (or the agent wrapping it) calls the embedding API once per document without chunking:

```python
import anthropic
import httpx

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: embed entire document as one vector
def embed_document(text: str) -> list[float]:
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": text},
        headers={"Authorization": "Bearer sk-live-..."}
    )
    return resp.json()["data"][0]["embedding"]
```

A single embedding for a long document is semantically lossy: the vector is pulled toward the most prominent topic, making precise retrieval for minority topics unreliable.

---

## Fix

### Option 1 — Fixed-size chunking with overlap before embedding

Split documents into fixed-size chunks with overlap to preserve context at chunk boundaries, then embed each chunk independently.

```python
import anthropic
import httpx
import json

client = anthropic.Anthropic(api_key="sk-live-...")

CHUNK_SIZE = 512    # tokens (approximate; use character count as proxy)
OVERLAP = 64        # tokens of overlap between adjacent chunks
CHARS_PER_TOKEN = 4  # rough approximation


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """Split text into overlapping fixed-size chunks."""
    chunk_chars = chunk_size * CHARS_PER_TOKEN
    overlap_chars = overlap * CHARS_PER_TOKEN
    step = chunk_chars - overlap_chars

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_chars
        chunks.append(text[start:end])
        start += step

    return chunks


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts in a single API call (batching reduces cost)."""
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": texts},
        headers={"Authorization": "Bearer sk-live-..."},
        timeout=30.0
    )
    data = resp.json()["data"]
    return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]


def embed_document_chunked(doc_id: str, text: str) -> list[dict]:
    """Return a list of chunk records with embeddings."""
    chunks = chunk_text(text)
    embeddings = embed_batch(chunks)

    print(f"[chunked] doc={doc_id}: {len(text)} chars → {len(chunks)} chunks")

    return [
        {
            "doc_id": doc_id,
            "chunk_index": i,
            "text": chunk,
            "embedding": emb,
            "char_start": i * (CHUNK_SIZE - OVERLAP) * CHARS_PER_TOKEN
        }
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
    ]


# Simulate embedding a document
sample_doc = "This is a legal contract section. " * 500  # ~17,000 chars
records = embed_document_chunked("contract_001", sample_doc)
print(f"Stored {len(records)} chunk embeddings")

# Token cost comparison (approximate):
# Full doc:  17,000 chars / 4 = 4,250 tokens → 1 embedding call
# Chunked:   512-token chunks → 9 chunks → 9 embeddings, but MUCH better retrieval

# Expected Token Savings: chunked embedding costs same or less; retrieval precision 2-5x better
# Environment: RAG agents indexing long documents (contracts, manuals, research papers)
```

---

### Option 2 — Sentence-boundary chunking for semantic coherence

Split at sentence boundaries rather than fixed character counts. Each chunk contains complete sentences, preserving meaning at chunk edges.

```python
import anthropic
import httpx
import re

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_CHUNK_TOKENS = 400
CHARS_PER_TOKEN = 4
MAX_CHUNK_CHARS = MAX_CHUNK_TOKENS * CHARS_PER_TOKEN


def split_sentences(text: str) -> list[str]:
    """Split text into sentences using simple regex."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


def sentence_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Group sentences into chunks without exceeding max_chars."""
    sentences = split_sentences(text)
    chunks = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)
        if current_len + sentence_len + 1 > max_chars and current:
            chunks.append(" ".join(current))
            # Overlap: keep last sentence in next chunk
            current = [current[-1], sentence]
            current_len = len(current[-2]) + sentence_len + 1
        else:
            current.append(sentence)
            current_len += sentence_len + 1

    if current:
        chunks.append(" ".join(current))

    return chunks


def embed_with_sentence_chunks(doc_id: str, text: str) -> list[dict]:
    chunks = sentence_chunks(text)

    # Batch all embeddings in one API call
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": chunks},
        headers={"Authorization": "Bearer sk-live-..."},
        timeout=30.0
    )
    embeddings = [item["embedding"] for item in sorted(resp.json()["data"], key=lambda x: x["index"])]

    print(f"[sentence-chunks] doc={doc_id}: {len(chunks)} chunks from {len(text)} chars")

    return [
        {"doc_id": doc_id, "chunk_index": i, "text": c, "embedding": e}
        for i, (c, e) in enumerate(zip(chunks, embeddings))
    ]


sample = ("The contract shall be governed by the laws of New York. "
          "Either party may terminate with 30 days notice. "
          "All disputes shall be resolved by arbitration. ") * 100

records = embed_with_sentence_chunks("agreement_007", sample)
print(f"Chunks: {len(records)}")

# Expected Token Savings: sentence-aware chunks are denser → fewer chunks than fixed-char splitting
# Environment: RAG over dense prose (legal, medical, academic documents)
```

---

### Option 3 — Hierarchical embedding: summary + chunk vectors

Embed both a document-level summary and individual chunks. Route queries to the right level: broad queries hit the summary; specific queries hit chunks.

```python
import anthropic
import httpx

client = anthropic.Anthropic(api_key="sk-live-...")


def summarise_for_embedding(text: str, max_summary_tokens: int = 200) -> str:
    """Ask Claude to produce a compact summary for document-level embedding."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_summary_tokens,
        system="Summarise the following document in 3-5 sentences for semantic search indexing. Be dense and factual.",
        messages=[{"role": "user", "content": text[:8000]}]  # First 8K chars for summary
    )
    return response.content[0].text.strip()


def embed_texts(texts: list[str]) -> list[list[float]]:
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": texts},
        headers={"Authorization": "Bearer sk-live-..."},
        timeout=30.0
    )
    return [item["embedding"] for item in sorted(resp.json()["data"], key=lambda x: x["index"])]


def hierarchical_embed(doc_id: str, text: str) -> dict:
    """
    Returns:
    - doc_record: document-level summary embedding (for broad retrieval)
    - chunk_records: chunk-level embeddings (for precise retrieval)
    """
    CHUNK_CHARS = 1600

    # 1. Generate summary
    summary = summarise_for_embedding(text)

    # 2. Split into chunks
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]

    # 3. Embed summary + all chunks in one batch
    all_texts = [summary] + chunks
    all_embeddings = embed_texts(all_texts)

    doc_embedding = all_embeddings[0]
    chunk_embeddings = all_embeddings[1:]

    print(f"[hierarchical] doc={doc_id}: 1 summary + {len(chunks)} chunks")

    return {
        "doc_record": {
            "doc_id": doc_id,
            "level": "document",
            "text": summary,
            "embedding": doc_embedding
        },
        "chunk_records": [
            {"doc_id": doc_id, "level": "chunk", "chunk_index": i, "text": c, "embedding": e}
            for i, (c, e) in enumerate(zip(chunks, chunk_embeddings))
        ]
    }


# Example (summary generation commented out for demo)
sample = "This product manual covers installation, safety, maintenance and troubleshooting. " * 200
# result = hierarchical_embed("manual_v3", sample)
print(f"[demo] Would embed 1 summary + {len(sample) // 1600 + 1} chunks")

# Expected Token Savings: summary enables fast pre-filtering; chunks only searched for relevant docs
# Environment: knowledge bases with many long documents; two-stage RAG pipelines
```

---

### Option 4 — Late chunking: embed full context, pool at chunk level

Embed a sliding window of context but pool embeddings at the chunk level, preserving global context without losing local precision.

```python
import anthropic
import httpx
import math

client = anthropic.Anthropic(api_key="sk-live-...")

WINDOW_SIZE = 1024     # chars per embedding window
CHUNK_SIZE = 256       # chars per retrievable chunk (smaller than window)
STRIDE = CHUNK_SIZE    # no overlap between retrievable chunks


def sliding_window_embed(doc_id: str, text: str) -> list[dict]:
    """
    Each retrievable chunk is embedded using a larger surrounding context window.
    This gives each chunk a context-aware embedding without embedding the full doc.
    """
    chunk_starts = list(range(0, len(text), STRIDE))
    records = []
    texts_to_embed = []

    for start in chunk_starts:
        chunk_end = start + CHUNK_SIZE
        chunk_text = text[start:chunk_end]

        # Window: extend backwards and forwards for context
        window_start = max(0, start - (WINDOW_SIZE - CHUNK_SIZE) // 2)
        window_end = min(len(text), window_start + WINDOW_SIZE)
        window_text = text[window_start:window_end]

        texts_to_embed.append(window_text)
        records.append({
            "doc_id": doc_id,
            "chunk_index": len(records),
            "chunk_text": chunk_text,
            "char_start": start,
            "char_end": chunk_end
        })

    # Batch all embeddings
    BATCH_SIZE = 100
    all_embeddings = []
    for i in range(0, len(texts_to_embed), BATCH_SIZE):
        batch = texts_to_embed[i:i + BATCH_SIZE]
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": batch},
            headers={"Authorization": "Bearer sk-live-..."},
            timeout=30.0
        )
        all_embeddings.extend(
            item["embedding"] for item in sorted(resp.json()["data"], key=lambda x: x["index"])
        )

    for record, embedding in zip(records, all_embeddings):
        record["embedding"] = embedding

    total_tokens_approx = len(texts_to_embed) * (WINDOW_SIZE // 4)
    print(f"[sliding-window] doc={doc_id}: {len(records)} chunks, ~{total_tokens_approx:,} embedding tokens")

    return records


sample = "Section " + ". Detailed explanation follows here" * 50
records = sliding_window_embed("spec_002", sample * 10)
print(f"Chunks stored: {len(records)}")

# Expected Token Savings: window embeddings use ~2x tokens of chunk size — far less than full doc
# Environment: technical documentation, code files, structured reports
```

---

### Option 5 — Cost-aware adaptive chunking based on document length

Choose chunk size dynamically based on document length: short documents embed whole; long documents chunk aggressively.

```python
import anthropic
import httpx

client = anthropic.Anthropic(api_key="sk-live-...")

# Thresholds: (max_chars, chunk_size_chars, overlap_chars)
CHUNK_POLICY = [
    (2_000,   2_000, 0),     # Short: embed whole document
    (8_000,   1_000, 100),   # Medium: 1K chunks
    (32_000,  512,   64),    # Long: 512-char chunks
    (float("inf"), 256, 32), # Very long: aggressive chunking
]


def adaptive_chunk(text: str) -> tuple[list[str], str]:
    """Choose chunk size based on document length. Returns (chunks, policy_used)."""
    doc_len = len(text)
    for max_chars, chunk_size, overlap in CHUNK_POLICY:
        if doc_len <= max_chars:
            if chunk_size >= doc_len:
                return [text], f"whole ({doc_len} chars)"
            step = chunk_size - overlap
            chunks = [text[i:i + chunk_size] for i in range(0, doc_len, step)]
            return chunks, f"{chunk_size}-char chunks ({len(chunks)} total)"
    return [text], "fallback"


def cost_estimate(chunks: list[str]) -> float:
    """Estimate embedding cost at $0.0001 / 1K tokens."""
    total_tokens = sum(len(c) // 4 for c in chunks)
    return total_tokens / 1000 * 0.0001


def embed_adaptive(doc_id: str, text: str) -> list[dict]:
    chunks, policy = adaptive_chunk(text)

    cost = cost_estimate(chunks)
    print(f"[adaptive] doc={doc_id}: {len(text):,} chars → {policy}, est. cost=${cost:.5f}")

    # Batch embed all chunks
    BATCH = 100
    embeddings = []
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": batch},
            headers={"Authorization": "Bearer sk-live-..."},
            timeout=30.0
        )
        embeddings.extend(
            item["embedding"] for item in sorted(resp.json()["data"], key=lambda x: x["index"])
        )

    return [
        {"doc_id": doc_id, "chunk_index": i, "text": c, "embedding": e}
        for i, (c, e) in enumerate(zip(chunks, embeddings))
    ]


# Demo (no real API calls)
for length, label in [(500, "tweet"), (5_000, "article"), (30_000, "report"), (100_000, "book")]:
    sample = "x" * length
    chunks, policy = adaptive_chunk(sample)
    cost = cost_estimate(chunks)
    print(f"{label:8s} ({length:>7,} chars): {policy}, est. ${cost:.5f}")

# Expected Token Savings: short docs embed whole (no overhead); long docs chunk aggressively
# Environment: mixed-length document corpora; cost-sensitive production RAG systems
```

---

### Option 6 — Claude-assisted intelligent chunk boundary detection

Ask Claude to identify natural section boundaries in a document, then embed each section as a coherent unit.

```python
import anthropic
import httpx
import json
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def detect_section_boundaries(text: str) -> list[int]:
    """Use Claude to identify where logical sections begin (returns char offsets)."""
    # Use first 4000 chars to detect structure pattern
    sample = text[:4000]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="""Analyse this document excerpt and identify logical section boundaries.
Return a JSON array of approximate character offsets where new sections begin.
Include offset 0 for the start. Focus on headings, numbered sections, or clear topic shifts.
Return ONLY a JSON array of integers, e.g.: [0, 450, 1200, 2100]""",
        messages=[{"role": "user", "content": f"Detect section boundaries:\n\n{sample}"}]
    )

    raw = response.content[0].text.strip()
    try:
        offsets = json.loads(raw)
        return sorted(set([0] + [int(o) for o in offsets if 0 < int(o) < len(text)]))
    except (json.JSONDecodeError, ValueError):
        # Fall back to every 1000 chars
        return list(range(0, len(text), 1000))


def embed_by_sections(doc_id: str, text: str) -> list[dict]:
    """Embed document using Claude-detected section boundaries."""
    boundaries = detect_section_boundaries(text)
    boundaries.append(len(text))  # Add end marker

    sections = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        section_text = text[start:end].strip()
        if section_text:
            sections.append(section_text)

    print(f"[llm-sections] doc={doc_id}: {len(sections)} sections detected")

    # Batch embed sections
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": sections},
        headers={"Authorization": "Bearer sk-live-..."},
        timeout=30.0
    )
    embeddings = [item["embedding"] for item in sorted(resp.json()["data"], key=lambda x: x["index"])]

    return [
        {"doc_id": doc_id, "section_index": i, "text": s, "embedding": e}
        for i, (s, e) in enumerate(zip(sections, embeddings))
    ]


# Demo without real API
sample_doc = """# Introduction
This manual covers safety and installation.

## Chapter 1: Safety
Always wear protective equipment.

## Chapter 2: Installation
Connect the power cable first. Then mount the bracket.

## Chapter 3: Maintenance
Clean filters monthly."""

boundaries = [0, 44, 75, 142]  # Simulated detection result
print(f"[demo] Would embed {len(boundaries)} sections from {len(sample_doc)} chars")

# Expected Token Savings: section-aware chunks are semantically complete → best retrieval quality
# Environment: structured documents (manuals, textbooks, reports with clear section hierarchy)
```

---

## Comparison

| Option | Boundary Strategy | Retrieval Quality | Cost vs Full-Doc | Complexity |
|--------|------------------|-------------------|-----------------|------------|
| 1 | Fixed size + overlap | Good | Same/lower | Low |
| 2 | Sentence boundary | Better | Same | Low |
| 3 | Hierarchical (summary + chunks) | Best (two-stage) | Higher | Medium |
| 4 | Sliding window (context-aware) | Very good | Moderate | Medium |
| 5 | Adaptive by doc length | Good | Lowest | Low |
| 6 | LLM-detected sections | Best (structure-aware) | Moderate | Medium |

**Recommended starting point:** Option 1 (fixed-size with overlap) for a first implementation — minimal code, works for any document type, and immediately improves retrieval over full-document embedding. Upgrade to Option 2 (sentence-boundary) for prose-heavy corpora and Option 3 (hierarchical) when you need both broad and precise retrieval in the same system.
