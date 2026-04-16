---
title: "Agent Doesn't Implement Document Chunking Strategy for Optimal Retrieval"
description: "AI agents that split documents by fixed character count produce chunks that cut across sentences, split code blocks mid-function, and lose structural context. Semantic chunking strategies — sentence-boundary splitting, recursive structural splitting, sliding-window overlap, and metadata-enriched chunking — preserve coherent units that embed and retrieve with higher precision than naive character splits."
date: 2025-02-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-document-chunking-strategy-for-optimal-retrieval
tags:
  - chunking
  - retrieval
  - rag
  - embedding
  - text-splitting
  - semantic-search
  - performance
symptoms:
  - "Retrieved chunks cut mid-sentence, making the LLM context incomplete"
  - "Code chunks split inside function bodies, breaking syntax"
  - "All chunks are exactly 512 chars regardless of sentence or paragraph boundaries"
  - "No overlap between chunks causes retrieval to miss context at chunk boundaries"
  - "Markdown headers and tables are split across multiple chunks losing structure"
---

## Problem

Fixed-size character chunking ignores document structure: a 500-character window may split a sentence in half, break a code block's indentation context, or separate a paragraph's topic sentence from its supporting detail. The embedded vector then represents a fragment rather than a coherent idea, reducing cosine similarity between query and relevant passage. Semantic chunking aligns chunk boundaries to linguistic and structural units — sentences, paragraphs, code blocks, markdown sections — producing embeddings that represent complete thoughts and retrieve with higher precision.

---

## Solution 1: SentenceBoundaryChunker — Split at Sentence Boundaries with Overlap

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Chunk:
    text: str
    start_char: int
    end_char: int
    chunk_index: int
    metadata: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.text)


class SentenceBoundaryChunker:
    """
    Splits text at sentence boundaries rather than fixed character offsets.
    Accumulates sentences until a target size is reached, then starts a
    new chunk with configurable sentence overlap to preserve cross-boundary context.

    Usage:
        chunker = SentenceBoundaryChunker(target_chars=800, overlap_sentences=1)
        chunks = chunker.chunk(document_text, metadata={"source": "paper.pdf"})
    """

    SENTENCE_PATTERN = re.compile(
        r'(?<=[.!?])\s+(?=[A-Z])|(?<=\n)\n+'
    )

    def __init__(self, target_chars: int = 800,
                  max_chars: int = 1200,
                  overlap_sentences: int = 1):
        self._target = target_chars
        self._max = max_chars
        self._overlap = overlap_sentences

    def _split_sentences(self, text: str) -> List[str]:
        sentences = self.SENTENCE_PATTERN.split(text)
        return [s.strip() for s in sentences if s.strip()]

    def chunk(self, text: str,
               metadata: Optional[dict] = None) -> List[Chunk]:
        sentences = self._split_sentences(text)
        chunks: List[Chunk] = []
        current: List[str] = []
        current_len = 0
        pos = 0
        chunk_idx = 0

        for sent in sentences:
            sent_len = len(sent)
            # If single sentence exceeds max, force-split it
            if sent_len > self._max:
                if current:
                    chunks.append(self._make_chunk(current, pos, chunk_idx, metadata))
                    pos += sum(len(s) + 1 for s in current)
                    chunk_idx += 1
                    current = current[-self._overlap:] if self._overlap else []
                    current_len = sum(len(s) for s in current)
                # Hard split the long sentence
                for i in range(0, sent_len, self._max - 50):
                    sub = sent[i:i + self._max - 50]
                    chunks.append(Chunk(
                        text=sub,
                        start_char=pos + i,
                        end_char=pos + i + len(sub),
                        chunk_index=chunk_idx,
                        metadata=metadata or {},
                    ))
                    chunk_idx += 1
                pos += sent_len + 1
                continue

            if current_len + sent_len > self._target and current:
                chunks.append(self._make_chunk(current, pos, chunk_idx, metadata))
                pos += sum(len(s) + 1 for s in current[:-self._overlap or None])
                chunk_idx += 1
                current = current[-self._overlap:] if self._overlap else []
                current_len = sum(len(s) for s in current)

            current.append(sent)
            current_len += sent_len

        if current:
            chunks.append(self._make_chunk(current, pos, chunk_idx, metadata))

        return chunks

    def _make_chunk(self, sentences: List[str], pos: int,
                     idx: int, metadata: Optional[dict]) -> Chunk:
        text = " ".join(sentences)
        return Chunk(
            text=text,
            start_char=pos,
            end_char=pos + len(text),
            chunk_index=idx,
            metadata=metadata or {},
        )
```

---

## Solution 2: RecursiveStructuralChunker — Respect Document Hierarchy

```python
import re
from typing import List, Optional, Tuple


class RecursiveStructuralChunker:
    """
    Recursively splits text by structural separators in priority order:
    double newlines (paragraphs) → single newlines → sentences → words.
    Falls back to the next separator only when the current level produces
    chunks that are still too large.

    Usage:
        chunker = RecursiveStructuralChunker(max_chars=1000, overlap_chars=100)
        chunks = chunker.chunk(long_document)
    """

    DEFAULT_SEPARATORS = [
        "\n\n",       # Paragraphs
        "\n",         # Lines
        ". ",         # Sentences
        "! ",
        "? ",
        "; ",
        ", ",
        " ",          # Words (last resort)
    ]

    def __init__(self, max_chars: int = 1000,
                  overlap_chars: int = 100,
                  separators: Optional[List[str]] = None):
        self._max = max_chars
        self._overlap = overlap_chars
        self._seps = separators or self.DEFAULT_SEPARATORS

    def chunk(self, text: str) -> List[Chunk]:
        raw = self._split(text, self._seps)
        return self._merge_with_overlap(raw)

    def _split(self, text: str, separators: List[str]) -> List[str]:
        if not separators:
            return [text[i:i + self._max] for i in range(0, len(text), self._max)]

        sep, rest_seps = separators[0], separators[1:]
        parts = text.split(sep)
        result = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(part) <= self._max:
                result.append(part)
            else:
                result.extend(self._split(part, rest_seps))
        return result

    def _merge_with_overlap(self, parts: List[str]) -> List[Chunk]:
        chunks: List[Chunk] = []
        current = ""
        idx = 0
        pos = 0

        for part in parts:
            if len(current) + len(part) + 1 > self._max and current:
                chunks.append(Chunk(
                    text=current.strip(),
                    start_char=pos,
                    end_char=pos + len(current),
                    chunk_index=idx,
                ))
                idx += 1
                # Keep overlap
                overlap_text = current[-self._overlap:] if self._overlap else ""
                pos += len(current) - len(overlap_text)
                current = overlap_text + " " + part
            else:
                current = (current + " " + part).strip() if current else part

        if current.strip():
            chunks.append(Chunk(
                text=current.strip(),
                start_char=pos,
                end_char=pos + len(current),
                chunk_index=idx,
            ))
        return chunks
```

---

## Solution 3: MarkdownAwareChunker — Preserve Headers and Code Blocks

```python
import re
from typing import List, Optional, Tuple


class MarkdownAwareChunker:
    """
    Chunks Markdown documents while preserving structural units:
    - Never splits inside fenced code blocks
    - Uses ATX headers (##) as natural section boundaries
    - Attaches the nearest parent header as metadata for each chunk

    Usage:
        chunker = MarkdownAwareChunker(max_chars=1200)
        chunks = chunker.chunk(markdown_text)
        # chunk.metadata["section"] contains the parent heading
    """

    HEADER_RE = re.compile(r'^(#{1,6})\s+(.*)', re.MULTILINE)
    CODE_FENCE_RE = re.compile(r'^```', re.MULTILINE)

    def __init__(self, max_chars: int = 1200, overlap_chars: int = 100):
        self._max = max_chars
        self._overlap = overlap_chars

    def chunk(self, text: str) -> List[Chunk]:
        sections = self._split_by_headers(text)
        chunks: List[Chunk] = []
        idx = 0

        for section_title, section_body in sections:
            safe_parts = self._split_respecting_code_fences(section_body)
            for part in safe_parts:
                if len(part) <= self._max:
                    chunks.append(Chunk(
                        text=part.strip(),
                        start_char=0,
                        end_char=len(part),
                        chunk_index=idx,
                        metadata={"section": section_title},
                    ))
                    idx += 1
                else:
                    sub_chunks = self._hard_split(part, section_title, idx)
                    chunks.extend(sub_chunks)
                    idx += len(sub_chunks)

        return chunks

    def _split_by_headers(self, text: str) -> List[Tuple[str, str]]:
        """Returns list of (header_title, body_text) pairs."""
        positions = [(m.start(), m.group(2)) for m in self.HEADER_RE.finditer(text)]
        if not positions:
            return [("", text)]
        sections = []
        for i, (pos, title) in enumerate(positions):
            end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            body = text[pos:end]
            sections.append((title, body))
        return sections

    def _split_respecting_code_fences(self, text: str) -> List[str]:
        """Split text by paragraphs without breaking code fences."""
        parts = []
        current = []
        in_fence = False
        for line in text.splitlines(keepends=True):
            if line.startswith("```"):
                in_fence = not in_fence
            if not in_fence and line.strip() == "" and current:
                block = "".join(current).strip()
                if block:
                    parts.append(block)
                current = []
            else:
                current.append(line)
        if current:
            parts.append("".join(current).strip())
        return [p for p in parts if p]

    def _hard_split(self, text: str, section: str, start_idx: int) -> List[Chunk]:
        chunks = []
        for i in range(0, len(text), self._max - self._overlap):
            sub = text[i:i + self._max]
            chunks.append(Chunk(
                text=sub,
                start_char=i,
                end_char=i + len(sub),
                chunk_index=start_idx + len(chunks),
                metadata={"section": section},
            ))
        return chunks
```

---

## Solution 4: SemanticSlidingWindowChunker — Cosine-Boundary Detection

```python
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class SemanticSlidingWindowChunker:
    """
    Uses embedding cosine similarity between adjacent sentences to detect
    topic shifts and place chunk boundaries at semantic discontinuities
    rather than fixed positions. Requires an embedding function.

    Usage:
        chunker = SemanticSlidingWindowChunker(
            embed_fn=embed,  # async fn(List[str]) -> List[List[float]]
            similarity_threshold=0.7,
            min_chunk_sentences=3,
        )
        chunks = await chunker.chunk(document_text)
    """

    def __init__(self, embed_fn,
                  similarity_threshold: float = 0.7,
                  min_chunk_sentences: int = 3,
                  max_chunk_sentences: int = 15,
                  window_size: int = 2):
        self._embed = embed_fn
        self._threshold = similarity_threshold
        self._min = min_chunk_sentences
        self._max = max_chunk_sentences
        self._window = window_size

    async def chunk(self, text: str,
                     metadata: Optional[dict] = None) -> List[Chunk]:
        import re
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        sentences = [s.strip() for s in sentences if s.strip()]

        if len(sentences) <= self._min:
            return [Chunk(text=text, start_char=0,
                          end_char=len(text), chunk_index=0,
                          metadata=metadata or {})]

        embeddings = await self._embed(sentences)
        boundaries = self._find_boundaries(embeddings, sentences)

        chunks: List[Chunk] = []
        start = 0
        for boundary in boundaries:
            group = sentences[start:boundary]
            if group:
                chunk_text = " ".join(group)
                chunks.append(Chunk(
                    text=chunk_text,
                    start_char=sum(len(s) + 1 for s in sentences[:start]),
                    end_char=sum(len(s) + 1 for s in sentences[:boundary]),
                    chunk_index=len(chunks),
                    metadata=metadata or {},
                ))
            start = boundary

        if start < len(sentences):
            chunk_text = " ".join(sentences[start:])
            chunks.append(Chunk(
                text=chunk_text,
                start_char=sum(len(s) + 1 for s in sentences[:start]),
                end_char=len(text),
                chunk_index=len(chunks),
                metadata=metadata or {},
            ))

        return chunks

    def _cosine(self, a: List[float], b: List[float]) -> float:
        import math
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x ** 2 for x in a))
        norm_b = math.sqrt(sum(x ** 2 for x in b))
        if not norm_a or not norm_b:
            return 0.0
        return dot / (norm_a * norm_b)

    def _find_boundaries(self, embeddings: List[List[float]],
                          sentences: List[str]) -> List[int]:
        boundaries = []
        current_count = 0
        for i in range(self._window, len(embeddings) - 1):
            sim = self._cosine(embeddings[i], embeddings[i + 1])
            current_count += 1
            if (sim < self._threshold and current_count >= self._min) \
                    or current_count >= self._max:
                boundaries.append(i + 1)
                current_count = 0
        return boundaries
```

---

## Solution 5: MetadataEnrichedChunker — Attach Document Context to Every Chunk

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EnrichedChunk:
    text: str
    chunk_index: int
    total_chunks: int
    doc_id: str
    source: str
    section: str
    page: Optional[int]
    created_at: float
    char_start: int
    char_end: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_embed_text(self) -> str:
        """Prepend structural context for richer embedding."""
        prefix_parts = []
        if self.source:
            prefix_parts.append(f"Source: {self.source}")
        if self.section:
            prefix_parts.append(f"Section: {self.section}")
        prefix = ". ".join(prefix_parts)
        return f"{prefix}. {self.text}" if prefix else self.text


class MetadataEnrichedChunker:
    """
    Wraps any base chunker and enriches each chunk with document-level
    metadata (source, doc_id, page, section, position) that is included
    in the embedding text prefix to improve retrieval precision.

    Usage:
        base = RecursiveStructuralChunker(max_chars=1000)
        enricher = MetadataEnrichedChunker(base)
        chunks = enricher.chunk(
            text=doc_text,
            source="whitepaper.pdf",
            section="Chapter 3",
            page=42,
        )
        texts_to_embed = [c.to_embed_text() for c in chunks]
    """

    def __init__(self, base_chunker):
        self._base = base_chunker

    def chunk(self, text: str,
               source: str = "",
               section: str = "",
               page: Optional[int] = None,
               extra_metadata: Optional[Dict[str, Any]] = None) -> List[EnrichedChunk]:
        base_chunks = self._base.chunk(text)
        doc_id = hashlib.sha256(text[:200].encode()).hexdigest()[:12]
        total = len(base_chunks)
        enriched = []

        for c in base_chunks:
            enriched.append(EnrichedChunk(
                text=c.text,
                chunk_index=c.chunk_index,
                total_chunks=total,
                doc_id=doc_id,
                source=source,
                section=section or c.metadata.get("section", ""),
                page=page,
                created_at=time.time(),
                char_start=c.start_char,
                char_end=c.end_char,
                metadata={**(c.metadata or {}), **(extra_metadata or {})},
            ))

        return enriched
```

---

## Solution 6: AdaptiveChunkingPipeline — Route Documents by Type

```python
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AdaptiveChunkingPipeline:
    """
    Routes documents to the appropriate chunker based on detected content type:
    - Markdown → MarkdownAwareChunker
    - Code files → fixed-size with no mid-line splits
    - General prose → SentenceBoundaryChunker
    - Short docs (<500 chars) → no chunking

    Usage:
        pipeline = AdaptiveChunkingPipeline()
        chunks = pipeline.process(
            text=document,
            filename="README.md",
            metadata={"source": "repo"},
        )
    """

    CODE_EXTENSIONS = {".py", ".ts", ".js", ".go", ".java", ".rs", ".cpp", ".c"}
    MARKDOWN_EXTENSIONS = {".md", ".mdx", ".markdown"}

    def __init__(self,
                  default_max_chars: int = 1000,
                  overlap_chars: int = 100):
        self._md_chunker = MarkdownAwareChunker(max_chars=default_max_chars)
        self._prose_chunker = SentenceBoundaryChunker(
            target_chars=default_max_chars,
            overlap_sentences=1,
        )
        self._struct_chunker = RecursiveStructuralChunker(
            max_chars=default_max_chars,
            overlap_chars=overlap_chars,
        )
        self._enricher = MetadataEnrichedChunker(self._prose_chunker)

    def _detect_type(self, text: str, filename: str) -> str:
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in self.MARKDOWN_EXTENSIONS:
            return "markdown"
        if ext in self.CODE_EXTENSIONS:
            return "code"
        if len(text) < 500:
            return "short"
        # Heuristic: check for markdown headers
        if re.search(r'^#{1,6}\s', text, re.MULTILINE):
            return "markdown"
        return "prose"

    def process(self, text: str,
                 filename: str = "",
                 metadata: Optional[Dict[str, Any]] = None) -> List[EnrichedChunk]:
        doc_type = self._detect_type(text, filename)
        logger.debug("chunking_strategy doc_type=%s filename=%s", doc_type, filename)

        if doc_type == "short":
            base_chunk = Chunk(text=text, start_char=0,
                               end_char=len(text), chunk_index=0)
            chunks = [base_chunk]
        elif doc_type == "markdown":
            chunks = self._md_chunker.chunk(text)
        elif doc_type == "code":
            # Split at function boundaries (blank lines between blocks)
            chunks = self._struct_chunker.chunk(text)
        else:
            chunks = self._prose_chunker.chunk(text, metadata=metadata)

        enricher = MetadataEnrichedChunker(
            type('_Passthrough', (), {
                'chunk': lambda self, t, **kw: chunks
            })()
        )
        return enricher.chunk(
            text,
            source=filename,
            extra_metadata={"doc_type": doc_type, **(metadata or {})},
        )
```

---

## Comparison

| Approach | Boundary Type | Code-Safe | Overlap | Metadata | Adaptive |
|---|---|---|---|---|---|
| **SentenceBoundaryChunker** | Sentence | No | Yes | Passthrough | No |
| **RecursiveStructuralChunker** | Structural | Partial | Yes | No | No |
| **MarkdownAwareChunker** | Header+Code | Yes | No | Section | No |
| **SemanticSlidingWindowChunker** | Semantic | No | No | Passthrough | No |
| **MetadataEnrichedChunker** | Wraps any | Via base | Via base | Yes | No |
| **AdaptiveChunkingPipeline** | By type | Yes | Yes | Yes | Yes |

**Key insight**: chunk boundary quality matters more than chunk size. A 600-character chunk that ends at a sentence boundary retrieves better than a 1000-character chunk that cuts mid-paragraph, because the embedding vector represents a coherent idea. Always set `overlap_sentences=1` or `overlap_chars=100` to prevent boundary misses — queries about content that straddles two chunks would otherwise return nothing. Use `MetadataEnrichedChunker` to prepend source and section to embedding text: a vector for "Source: API docs. Section: Authentication. Bearer token must be included..." will match authentication queries even when the chunk text alone is ambiguous.
