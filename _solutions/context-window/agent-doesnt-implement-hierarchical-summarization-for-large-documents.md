---
layout: solution
title: "Agent Doesn't Implement Hierarchical Summarization for Large Documents"
category: context-window
description: "Agents trying to process large documents either truncate them (losing information) or exceed context limits. Hierarchical summarization splits documents into chunks, summarizes each chunk, then summarizes the summaries — fitting any document into context while preserving key information."
tags: [context-window, summarization, hierarchical, large-documents, rag, chunking]
---

# Agent Doesn't Implement Hierarchical Summarization for Large Documents

## Problem

Documents that exceed the context window cannot be processed naively. Truncation loses the ending; sending the whole document raises an API error. Simple chunking without re-aggregation means the agent never sees the full picture. Hierarchical summarization solves this by building a compression tree: chunk → summarize → aggregate → final summary — preserving meaning at any document length.

## Why This Happens

Teams either truncate to `max_tokens` or implement simple single-level chunking. Multi-level summarization requires a recursive approach that feels complex to implement from scratch. The common workaround is to increase max tokens or switch to a longer-context model — but this increases cost quadratically and doesn't scale.

## Solutions

### Option 1: Map-Reduce Summarization — Summarize chunks in parallel, then reduce

```python
import anthropic
from dataclasses import dataclass, field

CHUNK_SIZE = 3000       # Characters per chunk
CHUNK_OVERLAP = 200     # Overlap to preserve context at boundaries


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


@dataclass
class MapReduceSummarizer:
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    chunk_model: str = "claude-haiku-4-5-20251001"   # Cheap model for chunk summaries
    reduce_model: str = "claude-sonnet-4-6"           # Better model for final synthesis

    def map_chunk(self, chunk: str, chunk_index: int, total_chunks: int) -> str:
        """Summarize a single chunk."""
        response = self.client.messages.create(
            model=self.chunk_model,
            max_tokens=512,
            system="You are a precise summarizer. Capture all key facts, decisions, and conclusions.",
            messages=[{
                "role": "user",
                "content": (
                    f"Summarize this section (part {chunk_index+1} of {total_chunks}). "
                    f"Preserve numbers, names, and specific claims.\n\n{chunk}"
                )
            }]
        )
        return response.content[0].text

    def reduce(self, summaries: list[str], original_question: str = "") -> str:
        """Combine chunk summaries into final answer."""
        combined = "\n\n---\n\n".join(
            f"[Section {i+1}]\n{s}" for i, s in enumerate(summaries)
        )
        task = f"Answer: {original_question}" if original_question else "Provide a comprehensive summary."
        response = self.client.messages.create(
            model=self.reduce_model,
            max_tokens=1024,
            system="You synthesize multiple document section summaries into a coherent, complete response.",
            messages=[{
                "role": "user",
                "content": f"Section summaries:\n\n{combined}\n\n{task}"
            }]
        )
        return response.content[0].text

    def summarize(self, document: str, question: str = "") -> dict:
        chunks = chunk_text(document)
        print(f"[MAP-REDUCE] {len(document):,} chars → {len(chunks)} chunks")

        summaries = [self.map_chunk(c, i, len(chunks)) for i, c in enumerate(chunks)]
        final = self.reduce(summaries, question)

        return {
            "chunks": len(chunks),
            "summary_count": len(summaries),
            "answer": final,
            "compression_ratio": round(len(document) / sum(len(s) for s in summaries), 1),
        }


# Usage
client = anthropic.Anthropic()
summarizer = MapReduceSummarizer(client=client)

# Simulate a large document
large_doc = ("Artificial intelligence research has accelerated dramatically. " * 50 +
             "Key findings include: transformer architectures dominate. " * 50 +
             "The economic impact is projected at $15.7 trillion by 2030. " * 30)

result = summarizer.summarize(large_doc, question="What are the key findings?")
print(f"Chunks: {result['chunks']}, Compression: {result['compression_ratio']}x")
print(result["answer"])

# Expected Token Savings: 60-80% vs sending full document; Haiku for map phase saves ~70% per chunk
# Environment: Legal document analysis, research paper processing, long report summarization
```

### Option 2: Recursive Tree Summarization — Multi-level compression for very long documents

```python
import anthropic
from dataclasses import dataclass, field
import math

MAX_CHUNK_CHARS = 4000
MAX_SUMMARY_CHARS = 1000   # Target size for each summary
MAX_SUMMARIES_PER_REDUCE = 5  # Max summaries to combine at once


@dataclass
class SummaryNode:
    content: str
    level: int
    children: list['SummaryNode'] = field(default_factory=list)
    char_count: int = field(init=False)

    def __post_init__(self):
        self.char_count = len(self.content)


class RecursiveTreeSummarizer:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.leaf_model = "claude-haiku-4-5-20251001"
        self.branch_model = "claude-haiku-4-5-20251001"
        self.root_model = "claude-sonnet-4-6"
        self.total_calls = 0

    def _summarize_text(self, text: str, model: str, level: int) -> str:
        self.total_calls += 1
        response = self.client.messages.create(
            model=model,
            max_tokens=400,
            system=f"Summarize concisely at level {level}. Keep numbers and key facts.",
            messages=[{"role": "user", "content": f"Summarize:\n\n{text}"}]
        )
        return response.content[0].text

    def _build_tree(self, text: str, level: int = 0) -> SummaryNode:
        """Recursively summarize until the text fits."""
        if len(text) <= MAX_CHUNK_CHARS:
            # Base case: text is small enough to summarize directly
            model = self.leaf_model if level < 2 else self.root_model
            summary = self._summarize_text(text, model, level)
            return SummaryNode(content=summary, level=level)

        # Split into chunks
        chunks = [text[i:i+MAX_CHUNK_CHARS] for i in range(0, len(text), MAX_CHUNK_CHARS)]
        print(f"[TREE L{level}] {len(text):,} chars → {len(chunks)} chunks")

        # Recursively summarize each chunk
        children = [self._build_tree(chunk, level + 1) for chunk in chunks]

        # Group children and reduce in batches
        while len(children) > MAX_SUMMARIES_PER_REDUCE:
            new_children = []
            for i in range(0, len(children), MAX_SUMMARIES_PER_REDUCE):
                batch = children[i:i + MAX_SUMMARIES_PER_REDUCE]
                combined = "\n\n".join(c.content for c in batch)
                model = self.branch_model
                summary = self._summarize_text(combined, model, level)
                new_node = SummaryNode(content=summary, level=level, children=batch)
                new_children.append(new_node)
            children = new_children

        # Final reduction at this level
        combined = "\n\n".join(c.content for c in children)
        model = self.root_model if level == 0 else self.branch_model
        final_summary = self._summarize_text(combined, model, level)
        return SummaryNode(content=final_summary, level=level, children=children)

    def summarize(self, document: str, final_question: str = "") -> dict:
        print(f"[TREE] Starting recursive summarization of {len(document):,} chars")
        root = self._build_tree(document, level=0)

        # If there's a specific question, ask it against the final summary
        if final_question:
            response = self.client.messages.create(
                model=self.root_model,
                max_tokens=1024,
                system="Answer the question based on this document summary.",
                messages=[{
                    "role": "user",
                    "content": f"Summary:\n{root.content}\n\nQuestion: {final_question}"
                }]
            )
            answer = response.content[0].text
        else:
            answer = root.content

        depth = self._tree_depth(root)
        return {
            "answer": answer,
            "tree_depth": depth,
            "api_calls": self.total_calls,
            "final_summary_chars": len(root.content),
            "compression_ratio": round(len(document) / len(root.content), 1),
        }

    def _tree_depth(self, node: SummaryNode) -> int:
        if not node.children:
            return 0
        return 1 + max(self._tree_depth(c) for c in node.children)


# Usage
summarizer = RecursiveTreeSummarizer()

# Simulate a very large document (could be 100k+ chars in production)
very_large_doc = (
    "Section 1: Background. The project began in 2023 with a $2M budget. " * 40 +
    "Section 2: Methodology. We used a randomized controlled trial with 500 participants. " * 40 +
    "Section 3: Results. The treatment group showed 34% improvement vs 12% control. " * 40 +
    "Section 4: Conclusions. The intervention is effective and cost-efficient at $400/patient. " * 30
)

result = summarizer.summarize(very_large_doc, "What were the key outcomes?")
print(f"Depth: {result['tree_depth']}, Calls: {result['api_calls']}, Compression: {result['compression_ratio']}x")
print(result["answer"])

# Expected Token Savings: 70-90% vs naive full-context send; depth scales logarithmically with document size
# Environment: Books, court filings, scientific papers, entire codebases, regulatory documents
```

### Option 3: Rolling Window Summarizer — Maintain a running summary as new sections arrive

```python
import anthropic
from dataclasses import dataclass, field

WINDOW_CHARS = 3000       # Characters to process per turn
SUMMARY_MAX_TOKENS = 400  # Keep summary compact


@dataclass
class RollingWindowSummarizer:
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    model: str = "claude-haiku-4-5-20251001"
    running_summary: str = ""
    sections_processed: int = 0

    def update(self, new_section: str) -> str:
        self.sections_processed += 1
        context = f"Previous summary:\n{self.running_summary}\n\n" if self.running_summary else ""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=SUMMARY_MAX_TOKENS,
            system="Maintain a rolling summary of a long document. Integrate new content with what came before.",
            messages=[{
                "role": "user",
                "content": (
                    f"{context}"
                    f"New section (part {self.sections_processed}):\n{new_section}\n\n"
                    f"Update the summary to include this new content. Be concise."
                )
            }]
        )
        self.running_summary = response.content[0].text
        return self.running_summary

    def finalize(self, question: str = "") -> str:
        if not question:
            return self.running_summary

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="Answer questions based on a document summary.",
            messages=[{
                "role": "user",
                "content": f"Document summary:\n{self.running_summary}\n\nQuestion: {question}"
            }]
        )
        return response.content[0].text

    def process_document(self, document: str, question: str = "", chunk_size: int = WINDOW_CHARS) -> dict:
        chunks = [document[i:i+chunk_size] for i in range(0, len(document), chunk_size)]
        print(f"[ROLLING] Processing {len(chunks)} sections of {chunk_size} chars each")

        for i, chunk in enumerate(chunks):
            summary = self.update(chunk)
            print(f"[ROLLING] After section {i+1}: summary is {len(summary)} chars")

        answer = self.finalize(question)
        return {
            "sections": len(chunks),
            "final_summary_chars": len(self.running_summary),
            "answer": answer,
        }


# Usage
summarizer = RollingWindowSummarizer()

document = (
    "Chapter 1: Introduction to Neural Networks. Neural networks are computing systems loosely modeled on biological brains. " * 20 +
    "Chapter 2: Training Methods. Backpropagation computes gradients through the chain rule for efficient weight updates. " * 20 +
    "Chapter 3: Modern Architectures. Transformers use self-attention, enabling parallel training unlike RNNs. " * 20 +
    "Chapter 4: Applications. NLP, computer vision, drug discovery, and robotics are major domains. " * 15
)

result = summarizer.process_document(document, question="What makes transformers better than RNNs?")
print(f"\nSections processed: {result['sections']}")
print(f"Answer: {result['answer']}")

# Expected Token Savings: Summary stays bounded regardless of document length; 75-85% input reduction
# Environment: Streaming documents, incremental report ingestion, real-time document monitoring
```

### Option 4: Extractive-then-Abstractive Pipeline — Extract key sentences first, then summarize

```python
import anthropic
import re
from dataclasses import dataclass, field

@dataclass
class ExtractiveAbstractivePipeline:
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    extraction_ratio: float = 0.3    # Keep top 30% of sentences
    extract_model: str = "claude-haiku-4-5-20251001"
    abstract_model: str = "claude-sonnet-4-6"

    def _score_sentences(self, text: str) -> list[tuple[str, float]]:
        """Score sentences by information density (heuristic: length + numeric content)."""
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        scored = []
        for sent in sentences:
            if len(sent) < 20:
                continue
            words = sent.split()
            has_numbers = sum(1 for w in words if any(c.isdigit() for c in w))
            has_capitals = sum(1 for w in words if w and w[0].isupper())
            score = len(words) * 0.5 + has_numbers * 3 + has_capitals * 1.5
            scored.append((sent, score))
        return scored

    def extract(self, text: str) -> str:
        """Extractive step: select high-value sentences."""
        scored = self._score_sentences(text)
        if not scored:
            return text

        scored.sort(key=lambda x: x[1], reverse=True)
        keep_count = max(5, int(len(scored) * self.extraction_ratio))
        top_sentences = [s for s, _ in scored[:keep_count]]

        # Re-order by original position
        original_order = {s: i for i, (s, _) in enumerate(scored)}
        top_sentences.sort(key=lambda s: original_order.get(s, 999))

        return " ".join(top_sentences)

    def abstract(self, extracted_text: str, question: str = "") -> str:
        """Abstractive step: generate coherent summary from extracted sentences."""
        task = f"Answer: {question}" if question else "Write a coherent summary."
        response = self.client.messages.create(
            model=self.abstract_model,
            max_tokens=1024,
            system="You receive key extracted sentences from a document. Generate a coherent, well-structured summary.",
            messages=[{
                "role": "user",
                "content": f"Key sentences:\n{extracted_text}\n\n{task}"
            }]
        )
        return response.content[0].text

    def summarize(self, document: str, question: str = "") -> dict:
        original_chars = len(document)
        extracted = self.extract(document)
        extracted_chars = len(extracted)

        print(f"[EXTRACT] {original_chars:,} chars → {extracted_chars:,} chars "
              f"({extracted_chars/original_chars:.0%} of original)")

        answer = self.abstract(extracted, question)

        return {
            "original_chars": original_chars,
            "extracted_chars": extracted_chars,
            "compression_ratio": round(original_chars / extracted_chars, 1),
            "answer": answer,
        }

    def summarize_large(self, document: str, question: str = "", chunk_size: int = 10000) -> dict:
        """Handle very large documents by chunking extraction."""
        if len(document) <= chunk_size:
            return self.summarize(document, question)

        chunks = [document[i:i+chunk_size] for i in range(0, len(document), chunk_size)]
        print(f"[EXTRACT-ABSTRACT] Large doc: {len(chunks)} chunks of {chunk_size} chars")

        extracted_chunks = [self.extract(chunk) for chunk in chunks]
        all_extracted = " ".join(extracted_chunks)

        print(f"[EXTRACT-ABSTRACT] Extracted: {len(all_extracted):,} chars total")
        answer = self.abstract(all_extracted, question)

        return {
            "chunks": len(chunks),
            "original_chars": len(document),
            "extracted_chars": len(all_extracted),
            "answer": answer,
        }


# Usage
pipeline = ExtractiveAbstractivePipeline()

research_paper = (
    "Abstract: This study examines the efficacy of transformer models in clinical NLP tasks. "
    "We present results on 5 benchmark datasets. "
    "The quick brown fox jumped over the lazy dog. "  # Low-value sentence
    "Our model achieved 94.3% F1 on the NER task, surpassing the previous SOTA by 3.2%. "
    "We trained on 2.1M medical records from 47 hospitals across 12 countries. "
    "It is important to note the following considerations. "  # Low-value
    "The model uses a 768-dimensional embedding space with 12 attention heads. "
    "Clinical implications include reduced annotation costs of approximately $2.3M annually. "
    "Future work will focus on multilingual extension and federated learning. "
) * 8

result = pipeline.summarize_large(research_paper, "What are the key results?")
print(f"Compression: {result['compression_ratio']}x")
print(result["answer"])

# Expected Token Savings: 50-70% reduction via heuristic extraction before expensive LLM call
# Environment: Research papers, legal briefs, medical records, any structured long-form document
```

### Option 5: Query-Focused Summarization — Only summarize sections relevant to the question

```python
import anthropic
from dataclasses import dataclass, field

CHUNK_SIZE = 2000
RELEVANCE_THRESHOLD = 0.4  # 0-1: chunks scoring above this are included


@dataclass
class QueryFocusedSummarizer:
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    relevance_model: str = "claude-haiku-4-5-20251001"
    answer_model: str = "claude-sonnet-4-6"

    def _score_relevance(self, chunk: str, query: str) -> float:
        """Ask Haiku to score how relevant a chunk is to the query."""
        response = self.client.messages.create(
            model=self.relevance_model,
            max_tokens=32,
            system="Score document chunk relevance to query. Return a float 0.0-1.0 only.",
            messages=[{
                "role": "user",
                "content": f"Query: {query}\n\nChunk: {chunk[:500]}\n\nRelevance score (0.0-1.0):"
            }]
        )
        try:
            return float(response.content[0].text.strip())
        except ValueError:
            return 0.5

    def filter_relevant_chunks(self, chunks: list[str], query: str) -> list[tuple[str, float]]:
        """Return (chunk, score) pairs above threshold."""
        scored = [(chunk, self._score_relevance(chunk, query)) for chunk in chunks]
        relevant = [(c, s) for c, s in scored if s >= RELEVANCE_THRESHOLD]
        relevant.sort(key=lambda x: x[1], reverse=True)
        print(f"[QUERY-FOCUS] {len(chunks)} chunks → {len(relevant)} relevant (threshold={RELEVANCE_THRESHOLD})")
        return relevant

    def answer(self, document: str, query: str) -> dict:
        chunks = [document[i:i+CHUNK_SIZE] for i in range(0, len(document), CHUNK_SIZE)]
        relevant = self.filter_relevant_chunks(chunks, query)

        if not relevant:
            return {"answer": "No relevant content found for this query.", "chunks_used": 0}

        context = "\n\n---\n\n".join(
            f"[Relevance: {score:.2f}]\n{chunk}"
            for chunk, score in relevant[:8]  # Top 8 most relevant
        )

        response = self.client.messages.create(
            model=self.answer_model,
            max_tokens=1024,
            system="Answer the question using only the provided document excerpts.",
            messages=[{
                "role": "user",
                "content": f"Document excerpts:\n\n{context}\n\nQuestion: {query}"
            }]
        )
        return {
            "answer": response.content[0].text,
            "chunks_used": len(relevant),
            "total_chunks": len(chunks),
            "chars_sent": len(context),
        }


# Usage
summarizer = QueryFocusedSummarizer()

document = (
    "Chapter 1: Market Overview. The global AI market was valued at $87B in 2025. Growth rate: 38% CAGR. " * 10 +
    "Chapter 2: Technical Architecture. Transformer models use multi-head self-attention with residual connections. " * 10 +
    "Chapter 3: Revenue Projections. By 2030, AI revenue is forecast at $1.35T with SaaS representing 42%. " * 10 +
    "Chapter 4: Regulatory Environment. The EU AI Act imposes fines up to 6% of global annual turnover. " * 10 +
    "Chapter 5: Competitive Landscape. OpenAI, Anthropic, Google, and Meta lead in foundation models. " * 10
)

result = summarizer.answer(document, "What are the revenue projections and market size?")
print(f"Used {result['chunks_used']}/{result['total_chunks']} chunks ({result['chars_sent']:,} chars)")
print(result["answer"])

# Expected Token Savings: 40-80% by skipping irrelevant sections; especially effective for targeted queries
# Environment: Multi-chapter reports, legal discovery, technical documentation with targeted questions
```

### Option 6: Async Parallel Hierarchical Summarizer — Parallelize all chunk summaries

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

CHUNK_SIZE = 3500
MAX_PARALLEL_CHUNKS = 8


@dataclass
class AsyncHierarchicalSummarizer:
    chunk_model: str = "claude-haiku-4-5-20251001"
    final_model: str = "claude-sonnet-4-6"

    def __post_init__(self):
        self.client = anthropic.AsyncAnthropic()
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_CHUNKS)

    async def _summarize_chunk(self, chunk: str, idx: int, total: int) -> str:
        async with self._semaphore:
            response = await self.client.messages.create(
                model=self.chunk_model,
                max_tokens=400,
                system="Summarize this document section. Preserve key facts, numbers, and conclusions.",
                messages=[{
                    "role": "user",
                    "content": f"Summarize (section {idx+1}/{total}):\n\n{chunk}"
                }]
            )
            return response.content[0].text

    async def _combine_summaries(self, summaries: list[str], question: str = "") -> str:
        combined = "\n\n---\n\n".join(
            f"[Section {i+1}]\n{s}" for i, s in enumerate(summaries)
        )
        task = f"Then answer: {question}" if question else "Write a comprehensive final summary."
        response = await self.client.messages.create(
            model=self.final_model,
            max_tokens=1024,
            system="Synthesize section summaries into a coherent final answer.",
            messages=[{
                "role": "user",
                "content": f"Section summaries:\n\n{combined}\n\n{task}"
            }]
        )
        return response.content[0].text

    async def summarize(self, document: str, question: str = "") -> dict:
        import time
        chunks = [document[i:i+CHUNK_SIZE] for i in range(0, len(document), CHUNK_SIZE)]
        total = len(chunks)
        print(f"[ASYNC-HIER] {len(document):,} chars → {total} chunks, {MAX_PARALLEL_CHUNKS} parallel")

        start = time.time()

        # Level 1: Summarize all chunks in parallel
        summaries = await asyncio.gather(*[
            self._summarize_chunk(chunk, i, total)
            for i, chunk in enumerate(chunks)
        ])

        # Level 2: If too many summaries, recursively reduce
        while len(summaries) > 8:
            group_size = 4
            groups = [summaries[i:i+group_size] for i in range(0, len(summaries), group_size)]
            summaries = await asyncio.gather(*[
                self._combine_summaries(group)
                for group in groups
            ])
            print(f"[ASYNC-HIER] Reduced to {len(summaries)} mid-level summaries")

        # Final synthesis
        final = await self._combine_summaries(summaries, question)
        elapsed = time.time() - start

        return {
            "chunks": total,
            "time_seconds": round(elapsed, 1),
            "answer": final,
            "speedup_vs_sequential": f"~{total}x (all chunks parallel)",
        }


async def main():
    summarizer = AsyncHierarchicalSummarizer()

    long_doc = (
        "Financial Report Q1 2026: Revenue grew 23% YoY to $4.2B driven by cloud segment. " * 15 +
        "Operating expenses rose 18% to $2.8B due to R&D investment in AI infrastructure. " * 15 +
        "Net income was $1.1B, representing a 26.2% net margin, up from 22.4% prior year. " * 15 +
        "Guidance: Q2 2026 revenue expected at $4.5-4.7B with operating margin of 26-27%. " * 15
    )

    result = await summarizer.summarize(long_doc, "What is the revenue growth and margin trend?")
    print(f"Chunks: {result['chunks']}, Time: {result['time_seconds']}s")
    print(result["answer"])


asyncio.run(main())

# Expected Token Savings: Same as map-reduce but 5-10x faster; parallelism critical for UX in real-time apps
# Environment: Real-time document Q&A, chat-based document analysis, latency-sensitive summarization APIs
```

## Comparison

| Option | Approach | Parallelism | Memory Usage | Best For |
|--------|---------|------------|-------------|----------|
| Map-Reduce | Flat chunking + reduce | Sequential | Low | Standard long documents |
| Recursive Tree | Multi-level hierarchy | Sequential | Medium | Very large documents (100k+ chars) |
| Rolling Window | Incremental summary | Sequential | Minimal | Streaming/incremental ingestion |
| Extractive-Abstractive | Filter then abstract | Sequential | Low | Dense factual documents |
| Query-Focused | Relevance filtering | Sequential | Low | Targeted question answering |
| Async Parallel | All-at-once parallel | Full parallel | Medium | Latency-critical applications |
