---
title: "Agent Doesn't Implement Document Chunking Strategy Optimization"
description: "Agents that chunk documents with a fixed character size and fixed overlap produce semantically incoherent chunks: a sentence split mid-way becomes unretrivable, a code block split across chunks confuses both halves, a table split at an arbitrary boundary loses its header row. Implement chunking strategy selection that chooses the appropriate boundary type (sentence, paragraph, code block, semantic section) based on content type to maximize chunk coherence and retrieval quality."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-document-chunking-strategy-optimization
tags: [chunking, document-processing, rag-optimization, semantic-boundaries, retrieval-quality, text-splitting]
symptoms:
  - "Retrieval returns half a sentence — the other half is in an adjacent chunk"
  - "Code blocks split across two chunks make both chunks semantically meaningless"
  - "Table header row separated from data rows — chunk with data has no column names"
  - "Fixed 500-character chunks produce widely varying semantic coherence"
  - "No measurement of chunk quality or boundary type distribution"
---

## Why This Happens

Fixed-size character chunking ignores semantic boundaries. A 500-character chunk might end mid-sentence, splitting a dependent clause from its context. A code block of 800 characters gets split into two fragments that are not independently meaningful. Optimal chunking requires detecting the content type (prose, code, table, list) and splitting at the appropriate boundary: sentences for prose, function boundaries for code, row groups for tables, item boundaries for lists. Chunk size is then a soft target, not a hard constraint.

## Solution 1: Chunk Boundary Type

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class BoundaryType(str, Enum):
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    SECTION = "section"          # markdown heading boundary
    CODE_BLOCK = "code_block"
    LIST_ITEM = "list_item"
    TABLE_ROW_GROUP = "table_row_group"
    FIXED_CHAR = "fixed_char"    # fallback


@dataclass
class DocumentChunk:
    content: str
    boundary_type: BoundaryType
    chunk_index: int
    token_estimate: int = 0
    source_document: str = ""
    metadata: dict = field(default_factory=dict)
    start_char: int = 0
    end_char: int = 0
```

## Solution 2: Content Type Detector

```python
import re


class DocumentContentTypeDetector:
    """
    Detects the dominant content type of a document or section.
    Returns the most appropriate chunking strategy.
    """

    CODE_FENCE = re.compile(r"^```", re.MULTILINE)
    TABLE_ROW = re.compile(r"^\|.+\|", re.MULTILINE)
    HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
    LIST_ITEM = re.compile(r"^[-*+]\s+\S|^\d+\.\s+\S", re.MULTILINE)

    def detect(self, text: str) -> BoundaryType:
        code_blocks = len(self.CODE_FENCE.findall(text))
        table_rows = len(self.TABLE_ROW.findall(text))
        headings = len(self.HEADING.findall(text))
        list_items = len(self.LIST_ITEM.findall(text))

        if code_blocks >= 2:    # at least one complete ``` ``` block
            return BoundaryType.CODE_BLOCK
        if table_rows >= 3:
            return BoundaryType.TABLE_ROW_GROUP
        if headings >= 2:
            return BoundaryType.SECTION
        if list_items >= 3:
            return BoundaryType.LIST_ITEM
        if len(text) > 2000:
            return BoundaryType.PARAGRAPH
        return BoundaryType.SENTENCE
```

## Solution 3: Semantic Boundary Splitter

```python
import re
from typing import List


class SemanticBoundarySplitter:
    """
    Splits text at semantic boundaries based on the detected content type.
    """

    def split(
        self,
        text: str,
        boundary_type: BoundaryType,
        target_tokens: int = 256,
        tokens_per_char: float = 0.25,
    ) -> List[str]:
        target_chars = int(target_tokens / tokens_per_char)

        if boundary_type == BoundaryType.SENTENCE:
            return self._split_sentences(text, target_chars)
        if boundary_type == BoundaryType.PARAGRAPH:
            return self._split_paragraphs(text, target_chars)
        if boundary_type == BoundaryType.SECTION:
            return self._split_sections(text, target_chars)
        if boundary_type == BoundaryType.CODE_BLOCK:
            return self._split_code_blocks(text, target_chars)
        if boundary_type == BoundaryType.TABLE_ROW_GROUP:
            return self._split_tables(text, target_chars)
        if boundary_type == BoundaryType.LIST_ITEM:
            return self._split_list_items(text, target_chars)
        return self._split_fixed(text, target_chars)

    def _split_sentences(self, text: str, max_chars: int) -> List[str]:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return self._group_to_size(sentences, max_chars)

    def _split_paragraphs(self, text: str, max_chars: int) -> List[str]:
        paragraphs = re.split(r'\n{2,}', text)
        return self._group_to_size(paragraphs, max_chars)

    def _split_sections(self, text: str, max_chars: int) -> List[str]:
        sections = re.split(r'(?=^#{1,6}\s+)', text, flags=re.MULTILINE)
        return [s.strip() for s in sections if s.strip()]

    def _split_code_blocks(self, text: str, max_chars: int) -> List[str]:
        parts = re.split(r'(```[\s\S]*?```)', text)
        chunks = []
        for part in parts:
            if not part.strip():
                continue
            if part.startswith("```"):
                chunks.append(part.strip())
            else:
                chunks.extend(self._split_paragraphs(part, max_chars))
        return chunks

    def _split_tables(self, text: str, max_chars: int) -> List[str]:
        lines = text.split('\n')
        header = []
        chunks = []
        current = []
        in_header = True

        for line in lines:
            if re.match(r'^\|.+\|', line):
                if in_header:
                    header.append(line)
                    if re.match(r'^\|[-| :]+\|', line):
                        in_header = False
                else:
                    current.append(line)
                    if len('\n'.join(header + current)) > max_chars:
                        chunks.append('\n'.join(header + current[:-1]))
                        current = [current[-1]]
            else:
                if current:
                    chunks.append('\n'.join(header + current))
                    current = []
                    in_header = False

        if current:
            chunks.append('\n'.join(header + current))
        return [c for c in chunks if c.strip()] or [text]

    def _split_list_items(self, text: str, max_chars: int) -> List[str]:
        items = re.split(r'\n(?=[-*+]\s+|\d+\.\s+)', text)
        return self._group_to_size(items, max_chars)

    def _split_fixed(self, text: str, max_chars: int) -> List[str]:
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    def _group_to_size(self, parts: List[str], max_chars: int) -> List[str]:
        chunks = []
        current = []
        current_len = 0
        for part in parts:
            if current_len + len(part) > max_chars and current:
                chunks.append(' '.join(current))
                current = [part]
                current_len = len(part)
            else:
                current.append(part)
                current_len += len(part)
        if current:
            chunks.append(' '.join(current))
        return chunks
```

## Solution 4: Adaptive Chunking Pipeline

```python
from typing import List


class AdaptiveChunkingPipeline:
    """
    Detects content type, selects the appropriate splitter, and
    produces DocumentChunk objects with boundary type metadata.
    """

    def __init__(
        self,
        detector: DocumentContentTypeDetector,
        splitter: SemanticBoundarySplitter,
        target_tokens: int = 256,
        tokens_per_char: float = 0.25,
    ):
        self._detector = detector
        self._splitter = splitter
        self._target_tokens = target_tokens
        self._tpc = tokens_per_char

    def chunk(self, text: str, source_document: str = "") -> List[DocumentChunk]:
        boundary_type = self._detector.detect(text)
        raw_chunks = self._splitter.split(text, boundary_type, self._target_tokens, self._tpc)

        result = []
        offset = 0
        for i, raw in enumerate(raw_chunks):
            if not raw.strip():
                continue
            token_est = max(1, int(len(raw) * self._tpc))
            result.append(DocumentChunk(
                content=raw.strip(),
                boundary_type=boundary_type,
                chunk_index=i,
                token_estimate=token_est,
                source_document=source_document,
                start_char=offset,
                end_char=offset + len(raw),
            ))
            offset += len(raw)
        return result
```

## Solution 5: Chunk Quality Evaluator

```python
import math
from typing import List


class ChunkQualityEvaluator:
    """
    Evaluates the quality of a set of chunks by measuring coherence
    indicators: token count distribution, sentence completeness, and
    boundary type consistency.
    """

    def evaluate(self, chunks: List[DocumentChunk]) -> dict:
        if not chunks:
            return {"chunks": 0}

        token_counts = [c.token_estimate for c in chunks]
        mean = sum(token_counts) / len(token_counts)
        variance = sum((t - mean) ** 2 for t in token_counts) / len(token_counts)
        cv = math.sqrt(variance) / max(mean, 1)   # coefficient of variation

        boundary_types = [c.boundary_type.value for c in chunks]
        dominant = max(set(boundary_types), key=boundary_types.count)

        # Penalize chunks that end mid-sentence (no terminal punctuation)
        incomplete = sum(
            1 for c in chunks
            if c.content and c.content[-1] not in ".!?:;\"'\n"
        )

        return {
            "chunk_count": len(chunks),
            "mean_tokens": round(mean, 1),
            "token_cv": round(cv, 3),
            "dominant_boundary_type": dominant,
            "incomplete_ending_chunks": incomplete,
            "quality_score": round(max(0.0, 1.0 - cv * 0.3 - incomplete / len(chunks) * 0.5), 3),
        }
```

## Solution 6: Chunking Strategy Comparison Dashboard

```python
import time
from typing import Dict, List


class ChunkingStrategyComparisonDashboard:
    """
    Runs multiple chunking strategies on the same document and
    compares their quality scores to recommend the best approach.
    """

    def __init__(
        self,
        pipeline: AdaptiveChunkingPipeline,
        evaluator: ChunkQualityEvaluator,
    ):
        self._pipeline = pipeline
        self._evaluator = evaluator

    def compare(self, text: str) -> dict:
        chunks = self._pipeline.chunk(text)
        quality = self._evaluator.evaluate(chunks)
        return {
            "generated_at": time.time(),
            "document_length": len(text),
            "boundary_type_used": quality.get("dominant_boundary_type"),
            "quality": quality,
            "sample_chunks": [
                {"index": c.chunk_index, "tokens": c.token_estimate, "preview": c.content[:100]}
                for c in chunks[:3]
            ],
        }
```

## Comparison

| Approach | Content Detection | Sentence Split | Section Split | Code Block Split | Table Split | Quality Score |
|---|---|---|---|---|---|---|
| DocumentContentTypeDetector | Yes | No | No | No | No | No |
| SemanticBoundarySplitter | No | Yes | Yes | Yes | Yes | No |
| AdaptiveChunkingPipeline | Via detector | Via splitter | Via splitter | Via splitter | Via splitter | No |
| ChunkQualityEvaluator | No | No | No | No | No | Yes |
| ChunkingStrategyComparisonDashboard | No | No | No | No | No | Via evaluator |

**Best for production**: Use `target_tokens=256` for retrieval-optimized RAG (dense retrieval models perform best at this range) and `target_tokens=512` for long-context models. Always preserve code blocks as atomic chunks — splitting a function across two chunks makes both unretrievable. For tables, always include the header row in every chunk — a data row without column headers is meaningless to a retriever and to the LLM. Monitor `quality_score` via `ChunkQualityEvaluator`: documents with scores below 0.7 indicate highly heterogeneous content (mixed prose, code, tables) that benefits from pre-splitting by section before chunking.
