---
title: "Agent Doesn't Implement Token-Aware Prompt Chunking for Large Documents"
description: "Agents that split documents by character count or line breaks produce chunks that overflow the context window or waste space with sparse splits. Implement token-aware chunking that measures chunk size in tokens, respects sentence and paragraph boundaries, generates configurable overlap between adjacent chunks, and builds a chunk index for targeted retrieval — enabling accurate and cost-efficient processing of arbitrarily large documents."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-token-aware-prompt-chunking-for-large-documents
tags: [chunking, token-counting, document-processing, context-window, retrieval, rag]
symptoms:
  - "Chunks split mid-sentence — model receives incomplete context and produces incoherent answers"
  - "Character-based split produces chunks of 800 tokens and 2400 tokens from the same document"
  - "Document processing fails with context-window overflow for PDFs over 50 pages"
  - "No overlap between chunks — information at chunk boundaries is never seen in full context"
  - "Agent re-chunks the same document on every session — no reuse of prior split results"
---

## Why This Happens

Text splitting by character count ignores tokenization: a 1000-character chunk might be 250 tokens or 600 tokens depending on content. Splitting at arbitrary points breaks sentences and disrupts meaning. Fixed-size splits with no overlap mean that information straddling a chunk boundary is never seen completely in any single call. Token-aware chunking uses an approximate token counter (4 chars ≈ 1 token, or a real tokenizer), respects natural boundaries (sentences, paragraphs, headings), and generates overlapping windows so adjacent chunks share context.

## Solution 1: Token Counter

```python
import re
from typing import List, Optional


class ApproximateTokenCounter:
    """
    Fast approximate token counter without requiring a full tokenizer.
    Uses the GPT rule of thumb: ~4 characters per token for English text.
    Override with a real tokenizer (tiktoken, HuggingFace) for accuracy.
    """

    CHARS_PER_TOKEN = 4.0

    def count(self, text: str) -> int:
        return max(1, int(len(text) / self.CHARS_PER_TOKEN))

    def count_batch(self, texts: List[str]) -> List[int]:
        return [self.count(t) for t in texts]

    def fits_in_budget(self, text: str, budget: int) -> bool:
        return self.count(text) <= budget


class TiktokenCounter:
    """
    Accurate token counter using tiktoken (install separately).
    Falls back to ApproximateTokenCounter if tiktoken is not installed.
    """

    def __init__(self, model: str = "gpt-4"):
        self._approx = ApproximateTokenCounter()
        self._enc = None
        try:
            import tiktoken
            self._enc = tiktoken.encoding_for_model(model)
        except Exception:
            pass

    def count(self, text: str) -> int:
        if self._enc:
            return len(self._enc.encode(text))
        return self._approx.count(text)

    def count_batch(self, texts: List[str]) -> List[int]:
        return [self.count(t) for t in texts]
```

## Solution 2: Document Chunk

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class DocumentChunk:
    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    token_count: int
    char_start: int
    char_end: int
    overlap_with_prev: int = 0    # tokens shared with previous chunk
    overlap_with_next: int = 0    # tokens shared with next chunk
    heading: str = ""             # nearest heading context
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def char_length(self) -> int:
        return self.char_end - self.char_start

    def content_hash(self) -> str:
        return hashlib.md5(self.text.encode()).hexdigest()[:8]
```

## Solution 3: Sentence-Boundary Splitter

```python
import re
from typing import List


class SentenceBoundarySplitter:
    """
    Splits text at sentence boundaries first, then assembles sentences
    into chunks that fit within a token budget with configurable overlap.
    Respects paragraph breaks (double newline) as hard split points.
    """

    # Sentence terminators: period/!/? followed by space or end-of-string
    SENTENCE_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z])|(?<=\n)\n+')

    def __init__(self, token_counter: ApproximateTokenCounter):
        self._tc = token_counter

    def split_into_sentences(self, text: str) -> List[str]:
        """Split text into sentence-level segments."""
        parts = self.SENTENCE_RE.split(text)
        return [p.strip() for p in parts if p.strip()]

    def assemble_chunks(
        self,
        sentences: List[str],
        max_tokens: int,
        overlap_tokens: int = 50,
    ) -> List[List[str]]:
        """
        Greedily pack sentences into chunks up to max_tokens.
        Each chunk overlaps with the previous by approximately overlap_tokens.
        Returns list of sentence groups, one per chunk.
        """
        chunks: List[List[str]] = []
        current: List[str] = []
        current_tokens = 0
        overlap_sentences: List[str] = []

        for sentence in sentences:
            s_tokens = self._tc.count(sentence)

            if current_tokens + s_tokens > max_tokens and current:
                chunks.append(current)
                # Compute overlap: keep trailing sentences up to overlap_tokens
                overlap: List[str] = []
                overlap_budget = overlap_tokens
                for s in reversed(current):
                    st = self._tc.count(s)
                    if overlap_budget - st < 0:
                        break
                    overlap.insert(0, s)
                    overlap_budget -= st
                current = list(overlap)
                current_tokens = sum(self._tc.count(s) for s in current)

            current.append(sentence)
            current_tokens += s_tokens

        if current:
            chunks.append(current)

        return chunks
```

## Solution 4: Token-Aware Document Chunker

```python
import hashlib
import time
import uuid
from typing import List, Optional


class TokenAwareDocumentChunker:
    """
    Full pipeline: detect headings, split at sentence boundaries,
    assemble token-bounded chunks with overlap, produce DocumentChunk objects.
    """

    HEADING_RE = __import__("re").compile(r'^#{1,6}\s+.+|^[A-Z][A-Z\s]{3,}$', __import__("re").MULTILINE)

    def __init__(
        self,
        token_counter: ApproximateTokenCounter,
        max_tokens_per_chunk: int = 512,
        overlap_tokens: int = 64,
    ):
        self._tc = token_counter
        self._max = max_tokens_per_chunk
        self._overlap = overlap_tokens
        self._splitter = SentenceBoundarySplitter(token_counter)

    def chunk(
        self,
        document_id: str,
        text: str,
        metadata: dict = None,
    ) -> List[DocumentChunk]:
        metadata = metadata or {}
        sentence_groups = self._splitter.assemble_chunks(
            self._splitter.split_into_sentences(text),
            max_tokens=self._max,
            overlap_tokens=self._overlap,
        )

        chunks = []
        char_pos = 0

        for idx, group in enumerate(sentence_groups):
            chunk_text = " ".join(group)
            token_count = self._tc.count(chunk_text)

            # Approximate char positions
            char_start = char_pos
            char_end = char_start + len(chunk_text)
            char_pos = char_end

            # Nearest heading
            heading = self._nearest_heading(text, char_start)

            chunk = DocumentChunk(
                chunk_id=f"{document_id}-{idx:04d}",
                document_id=document_id,
                chunk_index=idx,
                text=chunk_text,
                token_count=token_count,
                char_start=char_start,
                char_end=char_end,
                overlap_with_prev=self._overlap if idx > 0 else 0,
                overlap_with_next=self._overlap if idx < len(sentence_groups) - 1 else 0,
                heading=heading,
                metadata=metadata,
            )
            chunks.append(chunk)

        return chunks

    def _nearest_heading(self, text: str, char_pos: int) -> str:
        best = ""
        for m in self.HEADING_RE.finditer(text):
            if m.start() <= char_pos:
                best = m.group().strip()
            else:
                break
        return best[:80]
```

## Solution 5: Chunk Index

```python
import time
from typing import Dict, List, Optional


class DocumentChunkIndex:
    """
    In-memory index of document chunks.
    Supports lookup by document_id, chunk_index, and content hash.
    Tracks which chunks have been used in prompts for cost attribution.
    """

    def __init__(self):
        self._by_doc: Dict[str, List[DocumentChunk]] = {}
        self._by_chunk_id: Dict[str, DocumentChunk] = {}
        self._usage_counts: Dict[str, int] = {}

    def index(self, chunks: List[DocumentChunk]) -> None:
        for chunk in chunks:
            self._by_doc.setdefault(chunk.document_id, []).append(chunk)
            self._by_chunk_id[chunk.chunk_id] = chunk

    def get_chunks(self, document_id: str) -> List[DocumentChunk]:
        return self._by_doc.get(document_id, [])

    def get_chunk(self, chunk_id: str) -> Optional[DocumentChunk]:
        return self._by_chunk_id.get(chunk_id)

    def record_usage(self, chunk_id: str) -> None:
        self._usage_counts[chunk_id] = self._usage_counts.get(chunk_id, 0) + 1

    def most_used_chunks(self, top_n: int = 10) -> List[DocumentChunk]:
        sorted_ids = sorted(
            self._usage_counts, key=self._usage_counts.get, reverse=True
        )[:top_n]
        return [self._by_chunk_id[cid] for cid in sorted_ids if cid in self._by_chunk_id]

    def document_stats(self, document_id: str) -> dict:
        chunks = self.get_chunks(document_id)
        if not chunks:
            return {"document_id": document_id, "chunk_count": 0}
        total_tokens = sum(c.token_count for c in chunks)
        return {
            "document_id": document_id,
            "chunk_count": len(chunks),
            "total_tokens": total_tokens,
            "avg_tokens_per_chunk": round(total_tokens / len(chunks), 1),
            "min_tokens": min(c.token_count for c in chunks),
            "max_tokens": max(c.token_count for c in chunks),
        }

    def stats(self) -> dict:
        total_chunks = len(self._by_chunk_id)
        total_docs = len(self._by_doc)
        total_tokens = sum(c.token_count for c in self._by_chunk_id.values())
        return {
            "indexed_documents": total_docs,
            "total_chunks": total_chunks,
            "total_tokens": total_tokens,
            "total_usages": sum(self._usage_counts.values()),
        }
```

## Solution 6: Chunking Quality Analyzer

```python
import statistics
from typing import List


class ChunkingQualityAnalyzer:
    """
    Validates that a chunking run produced well-formed, evenly-sized chunks
    within the target token budget. Reports quality metrics and warns about
    outliers (very small or very large chunks).
    """

    def __init__(
        self,
        target_tokens: int,
        tolerance_pct: float = 0.20,
    ):
        self._target = target_tokens
        self._tolerance = tolerance_pct

    def analyze(self, chunks: List[DocumentChunk]) -> dict:
        if not chunks:
            return {"chunk_count": 0}

        token_counts = [c.token_count for c in chunks]
        mean = statistics.mean(token_counts)
        stdev = statistics.stdev(token_counts) if len(chunks) > 1 else 0.0
        p50 = statistics.median(token_counts)
        over_budget = [c for c in chunks if c.token_count > self._target]
        tiny = [c for c in chunks if c.token_count < self._target * 0.1]

        warnings = []
        if over_budget:
            warnings.append(
                f"{len(over_budget)} chunks exceed target {self._target} tokens"
            )
        if tiny:
            warnings.append(f"{len(tiny)} chunks are <10% of target size (possible split artifact)")
        cv = stdev / max(mean, 1)
        if cv > 0.5:
            warnings.append(f"high token count variance (CV={cv:.2f}) — consider tighter splitting")

        return {
            "chunk_count": len(chunks),
            "target_tokens": self._target,
            "mean_tokens": round(mean, 1),
            "stdev_tokens": round(stdev, 1),
            "p50_tokens": round(p50, 1),
            "min_tokens": min(token_counts),
            "max_tokens": max(token_counts),
            "over_budget_count": len(over_budget),
            "tiny_chunk_count": len(tiny),
            "warnings": warnings,
            "quality": "good" if not warnings else "needs_tuning",
        }
```

## Comparison

| Approach | Token Counting | Sentence Boundaries | Overlap | Chunk Index | Quality Analysis |
|---|---|---|---|---|---|
| ApproximateTokenCounter | Yes (fast) | No | No | No | No |
| SentenceBoundarySplitter | Via counter | Yes | Yes | No | No |
| TokenAwareDocumentChunker | Yes | Yes | Yes | No | No |
| DocumentChunkIndex | No | No | No | Yes | No |
| ChunkingQualityAnalyzer | No | No | No | No | Yes |

**Best for production**: Set `max_tokens_per_chunk` to 40–50% of the context window so two chunks plus a question fit in one call. Use `overlap_tokens=64` as a starting point — increase if your documents have dense cross-sentence references. Run `ChunkingQualityAnalyzer.analyze()` after chunking each new document to detect splitting artifacts before indexing. Cache chunks in `DocumentChunkIndex` keyed by `document_id` + content hash — re-chunk only when the document changes, not on every session.
