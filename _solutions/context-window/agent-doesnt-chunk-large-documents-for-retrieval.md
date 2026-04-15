---
layout: solution
title: "Agent doesn't chunk large documents for retrieval"
category: context-window
description: "Agent sends entire large documents to the model when answering document-based questions. A 50-page PDF consumes 40,000+ tokens even when the answer is in a single paragraph. Chunking and retrieving only the relevant sections reduces cost by 95%+ for document Q&A."
tags: [context-window, chunking, retrieval, rag, documents, token-cost]
---

## Symptom

The agent loads an entire PDF, legal contract, or codebase into the context window for every question about it. Input token counts are enormous. Long documents hit the context limit entirely. Shorter documents fit but the model's attention is diluted across irrelevant pages, reducing answer quality.

## Root Cause

Document loading code reads the full file and appends it to the prompt. There is no extraction step that identifies which paragraphs, sections, or pages are actually relevant to the question. The model receives 40,000 tokens of context to answer a question that requires 400 tokens of evidence.

## Fix

Chunk the document at load time. At query time, retrieve only the chunks most relevant to the question — using keyword overlap, TF-IDF, or embeddings similarity — and inject only those chunks into the prompt.

---

### Option 1 — Fixed-size chunking with keyword overlap retrieval

```python
import anthropic
import re
from typing import Sequence

client = anthropic.Anthropic(api_key="sk-live-...")


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping fixed-size character chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        # Snap to word boundary
        if end < len(text):
            snap = text.rfind(" ", start, end)
            if snap > start:
                end = snap
        chunks.append(text[start:end].strip())
        start = end - overlap
    return [c for c in chunks if c]


def keyword_score(query: str, chunk: str) -> int:
    """Count how many query words appear in the chunk."""
    query_words = set(re.findall(r"\b\w{3,}\b", query.lower()))
    chunk_lower = chunk.lower()
    return sum(1 for w in query_words if w in chunk_lower)


def retrieve_chunks(query: str, chunks: list[str], top_k: int = 3) -> list[str]:
    """Return the top_k chunks most relevant to the query by keyword overlap."""
    scored = [(keyword_score(query, c), c) for c in chunks]
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k] if _ > 0]


def run_doc_qa(document: str, question: str, top_k: int = 3) -> str:
    chunks = chunk_text(document, chunk_size=600, overlap=60)
    relevant = retrieve_chunks(question, chunks, top_k=top_k)

    if not relevant:
        relevant = chunks[:top_k]   # fallback: use first N chunks

    context = "\n\n---\n\n".join(relevant)
    total_doc_chars = len(document)
    used_chars = len(context)
    print(f"Using {used_chars}/{total_doc_chars} chars ({round(used_chars/total_doc_chars*100)}% of document)")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "You are a document Q&A assistant. "
            "Answer the question using only the provided document excerpts. "
            "If the answer is not in the excerpts, say so."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Document excerpts:\n{context}\n\nQuestion: {question}",
            }
        ],
    )
    return response.content[0].text
```

**Expected Token Savings:** A 40,000-token document reduced to 3 × 150-token chunks = 450 tokens; ~98 % reduction in document-related input tokens.
**Environment:** Any document Q&A agent; keyword retrieval requires no external dependencies and handles most cases well.

---

### Option 2 — Sentence-boundary chunking with TF-IDF retrieval

```python
import anthropic
import re
import math
from collections import Counter

client = anthropic.Anthropic(api_key="sk-live-...")


def sentence_chunks(text: str, sentences_per_chunk: int = 5) -> list[str]:
    """Split into chunks at sentence boundaries."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [
        " ".join(sentences[i:i + sentences_per_chunk])
        for i in range(0, len(sentences), sentences_per_chunk)
        if sentences[i:i + sentences_per_chunk]
    ]


def tfidf_score(query_terms: list[str], chunk: str, all_chunks: list[str]) -> float:
    """Compute a simple TF-IDF score for the chunk against the query."""
    chunk_words = re.findall(r"\b\w{3,}\b", chunk.lower())
    chunk_count = Counter(chunk_words)
    total_chunks = len(all_chunks)
    score = 0.0

    for term in query_terms:
        tf = chunk_count.get(term, 0) / max(len(chunk_words), 1)
        df = sum(1 for c in all_chunks if term in c.lower())
        idf = math.log((total_chunks + 1) / (df + 1)) + 1
        score += tf * idf

    return score


def retrieve_by_tfidf(query: str, chunks: list[str], top_k: int = 4) -> list[str]:
    query_terms = re.findall(r"\b\w{3,}\b", query.lower())
    if not query_terms:
        return chunks[:top_k]

    scored = [(tfidf_score(query_terms, c, chunks), c) for c in chunks]
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]


def run_doc_qa(document: str, question: str) -> str:
    chunks = sentence_chunks(document, sentences_per_chunk=6)
    relevant = retrieve_by_tfidf(question, chunks, top_k=4)
    context = "\n\n".join(f"[Excerpt {i+1}]\n{c}" for i, c in enumerate(relevant))

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Answer based only on the provided document excerpts. Cite excerpt numbers.",
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {question}"}],
    )
    return response.content[0].text
```

**Expected Token Savings:** TF-IDF scoring improves retrieval relevance over keyword overlap; same ~95–98 % token reduction with better precision on technical documents.
**Environment:** Technical documentation, legal documents, or research papers where terminology matters; TF-IDF rewards rare domain-specific terms over common words.

---

### Option 3 — Hierarchical chunking: section → paragraph → sentence

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def hierarchical_chunks(text: str) -> dict[str, list[str]]:
    """
    Build a three-level chunk hierarchy:
    - sections: split by headings (##, ###, or ALL CAPS lines)
    - paragraphs: split each section by double newlines
    - sentences: split each paragraph into sentences
    """
    # Level 1: sections
    section_pattern = re.compile(r"(?:^#{1,3}\s.+$|^[A-Z][A-Z\s]{5,}$)", re.MULTILINE)
    section_splits = section_pattern.split(text)
    section_titles = section_pattern.findall(text)

    sections: dict[str, str] = {}
    for i, content in enumerate(section_splits):
        title = section_titles[i - 1].strip() if i > 0 and i - 1 < len(section_titles) else f"Section {i}"
        sections[title] = content.strip()

    # Level 2 + 3: paragraphs and sentences per section
    hierarchy: dict[str, list[str]] = {"sections": [], "paragraphs": [], "sentences": []}

    for title, content in sections.items():
        if content:
            hierarchy["sections"].append(f"{title}\n{content[:500]}")

        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        for para in paragraphs:
            if len(para) > 50:
                hierarchy["paragraphs"].append(para)

            sentences = re.split(r"(?<=[.!?])\s+", para)
            hierarchy["sentences"].extend([s.strip() for s in sentences if len(s.strip()) > 30])

    return hierarchy


def keyword_score(query: str, text: str) -> int:
    words = set(re.findall(r"\b\w{3,}\b", query.lower()))
    return sum(1 for w in words if w in text.lower())


def run_hierarchical_qa(document: str, question: str) -> str:
    hierarchy = hierarchical_chunks(document)

    # Start with sections to identify the right part of the document
    top_sections = sorted(hierarchy["sections"], key=lambda c: -keyword_score(question, c))[:2]

    # Then drill into paragraphs within those sections
    candidate_paras = [
        p for p in hierarchy["paragraphs"]
        if any(p[:100] in s for s in top_sections) or keyword_score(question, p) > 0
    ]
    top_paras = sorted(candidate_paras, key=lambda c: -keyword_score(question, c))[:4]

    context = "\n\n".join(top_paras) or "\n\n".join(top_sections[:3])

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Answer based on the provided document excerpts.",
        messages=[{"role": "user", "content": f"Excerpts:\n{context}\n\nQuestion: {question}"}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Two-level retrieval (section then paragraph) improves precision; fewer irrelevant paragraphs are injected compared to flat chunk retrieval.
**Environment:** Structured documents with clear section headings (reports, contracts, manuals); hierarchical retrieval aligns with the document's natural organization.

---

### Option 4 — Async parallel chunking and retrieval

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class Chunk:
    index: int
    text: str
    score: float = 0.0


def make_chunks(text: str, size: int = 800, overlap: int = 80) -> list[Chunk]:
    chunks = []
    start = 0
    idx = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            snap = text.rfind("\n", start, end)
            if snap > start:
                end = snap
        chunks.append(Chunk(index=idx, text=text[start:end].strip()))
        start = end - overlap
        idx += 1
    return chunks


async def score_chunk(chunk: Chunk, query: str) -> Chunk:
    """Ask a cheap model to score this chunk's relevance to the query (0-10)."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        system="Rate the relevance of this text excerpt to the query on a scale 0-10. Reply with only the number.",
        messages=[{"role": "user", "content": f"Query: {query}\n\nExcerpt: {chunk.text[:400]}"}],
    )
    try:
        chunk.score = float(response.content[0].text.strip())
    except ValueError:
        chunk.score = 0.0
    return chunk


async def retrieve_chunks_async(
    document: str,
    question: str,
    top_k: int = 4,
    max_chunks_to_score: int = 20,
) -> list[Chunk]:
    chunks = make_chunks(document)

    # Fast keyword pre-filter to reduce scoring cost
    query_words = set(re.findall(r"\b\w{3,}\b", question.lower()))
    pre_filtered = sorted(
        chunks,
        key=lambda c: sum(1 for w in query_words if w in c.text.lower()),
        reverse=True,
    )[:max_chunks_to_score]

    # Score top candidates concurrently
    scored = await asyncio.gather(*[score_chunk(c, question) for c in pre_filtered])
    return sorted(scored, key=lambda c: -c.score)[:top_k]


async def run_doc_qa_async(document: str, question: str) -> str:
    relevant = await retrieve_chunks_async(document, question, top_k=4)
    context = "\n\n---\n\n".join(c.text for c in relevant)

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Answer based on the document excerpts. Acknowledge if the answer isn't present.",
        messages=[{"role": "user", "content": f"Excerpts:\n{context}\n\nQuestion: {question}"}],
    )
    return response.content[0].text


asyncio.run(run_doc_qa_async("... large document text ...", "What is the refund policy?"))
```

**Expected Token Savings:** Pre-filtering with keywords reduces scoring calls; concurrent scoring means retrieval latency is bounded by the slowest single chunk score rather than N × score_time.
**Environment:** Async agents with large documents where retrieval quality matters more than minimizing API calls; the Haiku scoring step costs ~20 tokens per chunk but substantially improves precision.

---

### Option 5 — Sliding window with context overlap for long-form Q&A

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_WINDOW_CHARS = 4000
STEP_CHARS = 3000   # overlap = MAX_WINDOW_CHARS - STEP_CHARS


def sliding_windows(text: str) -> list[tuple[int, str]]:
    """Yield (window_index, window_text) pairs with overlap."""
    windows = []
    for i, start in enumerate(range(0, len(text), STEP_CHARS)):
        end = min(start + MAX_WINDOW_CHARS, len(text))
        windows.append((i, text[start:end]))
        if end == len(text):
            break
    return windows


def find_answer_in_windows(question: str, windows: list[tuple[int, str]]) -> str | None:
    """
    Scan windows sequentially. Stop as soon as the model finds the answer.
    Returns the answer or None if not found in any window.
    """
    for window_idx, window_text in windows:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=(
                "You are searching a document for the answer to a question. "
                "If this excerpt contains the answer, provide it. "
                "If not, reply with exactly: NOT_FOUND"
            ),
            messages=[{
                "role": "user",
                "content": f"Question: {question}\n\nDocument excerpt {window_idx + 1}:\n{window_text}",
            }],
        )
        answer = response.content[0].text.strip()
        if answer != "NOT_FOUND" and "NOT_FOUND" not in answer:
            print(f"Found answer in window {window_idx + 1}")
            return answer

    return None


def run_doc_qa_sliding(document: str, question: str) -> str:
    windows = sliding_windows(document)
    print(f"Scanning {len(windows)} windows for: {question[:60]}")

    answer = find_answer_in_windows(question, windows)
    if answer:
        return answer

    # Fallback: ask with the first few windows combined
    first_context = "\n\n".join(w for _, w in windows[:3])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Answer based on the document excerpt. If unknown, say so.",
        messages=[{"role": "user", "content": f"Excerpts:\n{first_context}\n\nQuestion: {question}"}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Early stopping means most queries are answered after scanning 1–3 windows; only worst-case queries scan all windows; each Haiku window scan costs ~200 tokens.
**Environment:** Documents where the answer is typically in a specific section; sliding windows with early stopping are better than flat retrieval when answer location is predictable.

---

### Option 6 — Chunk index built at document load time (reused across queries)

```python
import anthropic
import re
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class DocumentIndex:
    doc_id: str
    chunks: list[str]
    chunk_words: list[set[str]]   # pre-computed word sets for fast scoring
    built_at: float = field(default_factory=time.time)
    total_chars: int = 0

    @classmethod
    def build(cls, doc_id: str, text: str, chunk_size: int = 600) -> "DocumentIndex":
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            snap = text.rfind(" ", start, end)
            if snap > start:
                end = snap
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end
        word_sets = [set(re.findall(r"\b\w{3,}\b", c.lower())) for c in chunks]
        return cls(
            doc_id=doc_id,
            chunks=chunks,
            chunk_words=word_sets,
            total_chars=len(text),
        )

    def retrieve(self, query: str, top_k: int = 4) -> list[str]:
        query_words = set(re.findall(r"\b\w{3,}\b", query.lower()))
        scored = [
            (len(query_words & words), chunk)
            for words, chunk in zip(self.chunk_words, self.chunks)
        ]
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k] if _ > 0] or self.chunks[:top_k]


# Document cache: build index once, reuse across queries
_doc_indices: dict[str, DocumentIndex] = {}


def get_or_build_index(doc_id: str, text: str) -> DocumentIndex:
    if doc_id not in _doc_indices:
        t0 = time.perf_counter()
        _doc_indices[doc_id] = DocumentIndex.build(doc_id, text)
        elapsed = time.perf_counter() - t0
        idx = _doc_indices[doc_id]
        print(f"Built index for {doc_id}: {len(idx.chunks)} chunks, {idx.total_chars} chars, {elapsed*1000:.1f}ms")
    return _doc_indices[doc_id]


def run_doc_qa(doc_id: str, document: str, question: str) -> str:
    idx = get_or_build_index(doc_id, document)
    relevant = idx.retrieve(question, top_k=4)
    context = "\n\n---\n\n".join(relevant)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Answer based on the provided document excerpts.",
        messages=[{"role": "user", "content": f"Excerpts:\n{context}\n\nQuestion: {question}"}],
    )
    return response.content[0].text


# Comparison table
# | Option | Chunking | Retrieval | Index Reuse |
# |--------|---------|-----------|-------------|
# | 1 Fixed-size + keyword | Char size | Word overlap | No |
# | 2 Sentence + TF-IDF | Sentence boundary | TF-IDF score | No |
# | 3 Hierarchical | Section/para/sentence | Multi-level | No |
# | 4 Async + LLM score | Char size | Haiku scoring | No |
# | 5 Sliding window | Overlapping windows | Early stopping | No |
# | 6 Pre-built index | Char size | Word set intersection | Yes |
```

**Expected Token Savings:** Index is built once (zero API cost); subsequent queries against the same document use cached word sets for O(n_chunks) retrieval with zero additional calls; amortized cost approaches zero for frequently-queried documents.
**Environment:** Agents that answer multiple questions about the same document; the index pays back its build cost after the first query.
