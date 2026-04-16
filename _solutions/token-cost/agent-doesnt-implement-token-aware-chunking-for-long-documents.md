---
layout: solution
title: "Agent Doesn't Implement Token-Aware Chunking for Long Documents"
category: token-cost
description: "Split long documents into token-budget-aware chunks before sending to the model — preventing context overflow, reducing per-call cost, and enabling overlap-based continuity without resending full documents."
tags: [token-cost, chunking, context-window, long-documents, cost-optimization, python]
---

# Agent Doesn't Implement Token-Aware Chunking for Long Documents

Agents that send entire documents in a single call either hit context limits silently or waste tokens on content the model cannot attend to effectively. Token-aware chunking splits documents at natural boundaries within a defined budget — enabling incremental processing, overlap for continuity, and per-chunk cost tracking without context overflow.

## Option 1: Character-Estimated Fixed-Size Chunking

```python
import anthropic

client = anthropic.Anthropic()

def estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 4 characters for English text."""
    return len(text) // 4

def chunk_by_token_budget(
    text: str,
    token_budget: int = 1500,
    overlap_tokens: int = 100,
) -> list[str]:
    """Split text into chunks that fit within token_budget, with overlap."""
    char_budget = token_budget * 4
    overlap_chars = overlap_tokens * 4
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + char_budget, len(text))
        # Snap to word boundary
        if end < len(text):
            snap = text.rfind(" ", start, end)
            if snap > start:
                end = snap
        chunks.append(text[start:end])
        start = end - overlap_chars  # overlap for continuity
        if start <= 0:
            break
    return chunks

def summarize_chunks(chunks: list[str]) -> list[str]:
    summaries = []
    total_in = total_out = 0
    for i, chunk in enumerate(chunks):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"Summarize this passage in 2-3 sentences:\n\n{chunk}",
            }],
        )
        summaries.append(resp.content[0].text)
        total_in  += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        print(f"  Chunk {i+1}/{len(chunks)}: "
              f"{estimate_tokens(chunk):4d} est. tokens -> "
              f"{resp.usage.input_tokens} actual in, {resp.usage.output_tokens} out")
    print(f"  Total: {total_in} in / {total_out} out tokens across {len(chunks)} chunks")
    return summaries

# Example: simulate a long document
document = " ".join([f"Sentence {i}: Python is a versatile programming language used for data science, web development, and automation." for i in range(200)])
print(f"Document: {len(document)} chars, ~{estimate_tokens(document)} tokens")
chunks = chunk_by_token_budget(document, token_budget=800, overlap_tokens=50)
print(f"Chunks: {len(chunks)}")
summaries = summarize_chunks(chunks)
print(f"\nFinal summary count: {len(summaries)}")

# Expected Token Savings: Chunking prevents single 50k-token call; distributes cost across manageable chunks
# Environment: character estimate works for English; use tiktoken or claude token counter for precision
```

## Option 2: Sentence-Boundary Chunking with Token Counting

```python
import anthropic
import re

client = anthropic.Anthropic()

def split_sentences(text: str) -> list[str]:
    """Split text into sentences at punctuation boundaries."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]

def chunk_by_sentences(
    sentences: list[str],
    token_budget: int = 1200,
    overlap_sentences: int = 2,
) -> list[list[str]]:
    """Group sentences into chunks respecting token_budget."""
    def est(s): return len(s) // 4

    chunks: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0

    for sent in sentences:
        sent_tokens = est(sent)
        if current and current_tokens + sent_tokens > token_budget:
            chunks.append(current[:])
            # Overlap: carry last N sentences into next chunk
            current = current[-overlap_sentences:] if overlap_sentences else []
            current_tokens = sum(est(s) for s in current)
        current.append(sent)
        current_tokens += sent_tokens

    if current:
        chunks.append(current)
    return chunks

def process_chunk(chunk_sentences: list[str], chunk_idx: int, total: int) -> dict:
    text = " ".join(chunk_sentences)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"Extract the 3 most important facts from this text:\n\n{text}",
        }],
    )
    return {
        "chunk": chunk_idx,
        "sentences": len(chunk_sentences),
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "facts": resp.content[0].text,
    }

# Simulate document with natural sentences
document = ". ".join([
    f"Python was created by Guido van Rossum in 1991",
    f"It emphasizes code readability and simplicity",
    f"Python supports multiple programming paradigms",
    f"The language has a large standard library",
    f"Asyncio enables concurrent I/O operations",
    f"Dataclasses simplify boilerplate for data containers",
    f"Type hints improve code clarity and tooling support",
    f"Virtual environments isolate project dependencies",
    f"The GIL limits true thread parallelism in CPython",
    f"NumPy and pandas are essential for data analysis",
] * 15)  # 150 sentences

sentences = split_sentences(document)
chunks = chunk_by_sentences(sentences, token_budget=600, overlap_sentences=1)
print(f"Document: {len(sentences)} sentences -> {len(chunks)} chunks")

total_in = total_out = 0
for i, chunk in enumerate(chunks):
    result = process_chunk(chunk, i + 1, len(chunks))
    total_in  += result["input_tokens"]
    total_out += result["output_tokens"]
    print(f"  Chunk {result['chunk']}: {result['sentences']} sentences, "
          f"{result['input_tokens']}in/{result['output_tokens']}out tokens")

print(f"\nTotal: {total_in} in + {total_out} out = {total_in + total_out} tokens")

# Expected Token Savings: Sentence boundaries prevent mid-sentence cuts; overlap avoids context loss
# Environment: re.split sentence splitter works for structured prose; use spacy for complex text
```

## Option 3: Hierarchical Chunking — Chunk then Reduce

```python
import anthropic

client = anthropic.Anthropic()

def estimate_tokens(text: str) -> int:
    return len(text) // 4

def split_paragraphs(text: str, token_budget: int = 1000) -> list[str]:
    """Split by paragraphs, merging short ones to fill budget."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    current = ""
    for para in paras:
        if estimate_tokens(current + "\n\n" + para) <= token_budget:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks

def summarize_one(text: str, instruction: str = "Summarize in 2 sentences.") -> tuple[str, int, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"{instruction}\n\n{text}"}],
    )
    return resp.content[0].text, resp.usage.input_tokens, resp.usage.output_tokens

def map_reduce_summarize(document: str, chunk_budget: int = 800) -> dict:
    """Map: summarize each chunk. Reduce: summarize all summaries."""
    chunks = split_paragraphs(document, token_budget=chunk_budget)
    print(f"Map phase: {len(chunks)} chunks")

    map_summaries = []
    map_in = map_out = 0
    for i, chunk in enumerate(chunks):
        summary, in_tok, out_tok = summarize_one(chunk)
        map_summaries.append(summary)
        map_in  += in_tok
        map_out += out_tok
        print(f"  Chunk {i+1}: {in_tok}in/{out_tok}out -> {summary[:50]!r}")

    # Reduce phase
    combined = "\n\n".join(map_summaries)
    print(f"\nReduce phase: {estimate_tokens(combined)} tokens")
    final, red_in, red_out = summarize_one(
        combined,
        instruction="Combine these summaries into one coherent paragraph:",
    )
    print(f"  Reduce: {red_in}in/{red_out}out")

    total = map_in + map_out + red_in + red_out
    return {
        "final_summary": final,
        "chunks": len(chunks),
        "map_tokens": map_in + map_out,
        "reduce_tokens": red_in + red_out,
        "total_tokens": total,
    }

# Simulate multi-paragraph document
document = "\n\n".join([
    f"Section {i}: " + "Python enables rapid development. " * 20
    for i in range(10)
])
result = map_reduce_summarize(document, chunk_budget=600)
print(f"\nFinal: {result['final_summary'][:100]}")
print(f"Cost: {result['total_tokens']} total tokens across {result['chunks']} chunks")

# Expected Token Savings: Map-reduce avoids single 20k-token call; reduce layer costs only summary tokens
# Environment: tune chunk_budget to model's effective attention span (~1k-2k for haiku)
```

## Option 4: Sliding Window with Context Carry-Over

```python
import anthropic

client = anthropic.Anthropic()

def estimate_tokens(text: str) -> int:
    return len(text) // 4

def sliding_window_process(
    document: str,
    window_tokens: int = 1000,
    step_tokens: int = 800,
    task: str = "Extract key facts from this passage:",
) -> list[dict]:
    """
    Process document with a sliding window.
    window_tokens: size of each window
    step_tokens: how far to advance (window - step = overlap)
    """
    chars_window = window_tokens * 4
    chars_step   = step_tokens   * 4
    results = []
    pos = 0
    window_idx = 0

    while pos < len(document):
        end = min(pos + chars_window, len(document))
        chunk = document[pos:end]
        context_note = f"[Window {window_idx+1}, chars {pos}-{end}]"

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": f"{task}\n\n{context_note}\n\n{chunk}",
            }],
        )
        results.append({
            "window": window_idx + 1,
            "pos": pos,
            "end": end,
            "chars": end - pos,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "result": resp.content[0].text,
        })
        print(f"  Window {window_idx+1}: chars {pos}-{end} | "
              f"{resp.usage.input_tokens}in/{resp.usage.output_tokens}out")

        pos += chars_step
        window_idx += 1
        if end == len(document):
            break

    total_in  = sum(r["input_tokens"]  for r in results)
    total_out = sum(r["output_tokens"] for r in results)
    overlap_pct = (1 - step_tokens / window_tokens) * 100
    print(f"\n{window_idx} windows | overlap: {overlap_pct:.0f}% | "
          f"total: {total_in}in / {total_out}out tokens")
    return results

document = "Python is a high-level programming language. " * 300  # ~6k chars
results = sliding_window_process(
    document,
    window_tokens=800,
    step_tokens=640,  # 20% overlap
    task="List 2 key points from this passage:",
)
print(f"\nExtracted {len(results)} result windows")

# Expected Token Savings: 20% overlap is enough for continuity; 50%+ overlap doubles cost with diminishing return
# Environment: tune step/window ratio; use 10-20% overlap for factual extraction, 30% for narrative tasks
```

## Option 5: Semantic Chunk Boundary Detection

```python
import anthropic
import re

client = anthropic.Anthropic()

def estimate_tokens(text: str) -> int:
    return len(text) // 4

def detect_section_boundaries(text: str) -> list[int]:
    """Find positions of semantic boundaries: headers, double newlines, numbered sections."""
    boundaries = [0]
    # Markdown headers
    for m in re.finditer(r"^#{1,3} .+$", text, re.MULTILINE):
        boundaries.append(m.start())
    # Double newlines (paragraph breaks)
    for m in re.finditer(r"\n\n+", text):
        boundaries.append(m.start())
    # Numbered sections like "1. " or "Section 1:"
    for m in re.finditer(r"(?m)^\d+[.)]\s+\w", text):
        boundaries.append(m.start())
    boundaries.append(len(text))
    return sorted(set(boundaries))

def semantic_chunks(
    text: str,
    token_budget: int = 1200,
) -> list[str]:
    """Chunk at semantic boundaries, merging small sections."""
    boundaries = detect_section_boundaries(text)
    sections = [text[boundaries[i]:boundaries[i+1]].strip()
                for i in range(len(boundaries) - 1)
                if boundaries[i] < boundaries[i+1]]
    sections = [s for s in sections if s]

    chunks = []
    current = ""
    for section in sections:
        candidate = (current + "\n\n" + section).strip()
        if estimate_tokens(candidate) <= token_budget:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If single section exceeds budget, split by sentences
            if estimate_tokens(section) > token_budget:
                sentences = re.split(r"(?<=[.!?])\s+", section)
                buf = ""
                for sent in sentences:
                    if estimate_tokens(buf + " " + sent) <= token_budget:
                        buf = (buf + " " + sent).strip()
                    else:
                        if buf:
                            chunks.append(buf)
                        buf = sent
                if buf:
                    chunks.append(buf)
                current = ""
            else:
                current = section

    if current:
        chunks.append(current)
    return chunks

document = """
# Introduction
Python is a high-level programming language designed for readability.

## Core Features
It supports multiple paradigms: procedural, object-oriented, and functional.

## Async Support
The asyncio module enables concurrent I/O without threads.

## Memory Management
Python uses automatic garbage collection via reference counting.

## Standard Library
Over 200 modules are included in the standard library.
""" * 8  # Repeat to create a long document

chunks = semantic_chunks(document, token_budget=600)
print(f"Document: {estimate_tokens(document)} tokens -> {len(chunks)} semantic chunks")

total_in = total_out = 0
for i, chunk in enumerate(chunks):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"One sentence summary:\n\n{chunk}"}],
    )
    total_in  += resp.usage.input_tokens
    total_out += resp.usage.output_tokens
    print(f"  Chunk {i+1} ({estimate_tokens(chunk):4d} tok): {resp.content[0].text[:60]!r}")

print(f"\nTotal: {total_in}in + {total_out}out tokens")

# Expected Token Savings: Semantic boundaries preserve coherent sections; no mid-topic cuts vs fixed-char splitting
# Environment: regex boundary detection works for markdown and numbered docs; extend for XML/HTML structure
```

## Option 6: Per-Chunk SQLite Cost Tracker with Budget Enforcement

```python
import anthropic
import sqlite3
import time
import sys

client = anthropic.Anthropic()
DB = "chunk_cost.db"
HAIKU_INPUT_COST  = 0.80 / 1_000_000   # $ per input token
HAIKU_OUTPUT_COST = 4.00 / 1_000_000   # $ per output token

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunk_runs (
            doc_id TEXT, chunk_idx INTEGER, char_start INTEGER, char_end INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cost_usd REAL, ts REAL
        )
    """)
    con.commit(); con.close()

def estimate_tokens(text: str) -> int:
    return len(text) // 4

def chunk_text(text: str, token_budget: int = 1000) -> list[tuple[int, int, str]]:
    """Return list of (char_start, char_end, chunk_text) tuples."""
    chars = token_budget * 4
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + chars, len(text))
        if end < len(text):
            snap = text.rfind(" ", pos, end)
            if snap > pos:
                end = snap
        chunks.append((pos, end, text[pos:end]))
        pos = end
    return chunks

def process_with_budget(
    doc_id: str,
    document: str,
    task: str,
    token_budget_per_chunk: int = 800,
    max_spend_usd: float = 0.01,
) -> list[str]:
    init_db()
    chunks = chunk_text(document, token_budget_per_chunk)
    print(f"Doc {doc_id}: {len(document)} chars -> {len(chunks)} chunks")
    print(f"Budget cap: ${max_spend_usd:.4f}")

    results = []
    total_spent = 0.0

    for i, (start, end, chunk) in enumerate(chunks):
        # Pre-flight cost estimate
        est_input = estimate_tokens(task) + estimate_tokens(chunk) + 20
        est_cost  = est_input * HAIKU_INPUT_COST + 150 * HAIKU_OUTPUT_COST
        if total_spent + est_cost > max_spend_usd:
            print(f"  [STOP] Budget cap reached at chunk {i+1}/{len(chunks)} "
                  f"(spent ${total_spent:.5f})")
            break

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"{task}\n\n{chunk}"}],
        )
        cost = (resp.usage.input_tokens  * HAIKU_INPUT_COST +
                resp.usage.output_tokens * HAIKU_OUTPUT_COST)
        total_spent += cost
        results.append(resp.content[0].text)

        con = sqlite3.connect(DB)
        con.execute(
            "INSERT INTO chunk_runs VALUES (?,?,?,?,?,?,?,?)",
            (doc_id, i, start, end,
             resp.usage.input_tokens, resp.usage.output_tokens,
             cost, time.time()),
        )
        con.commit(); con.close()
        print(f"  Chunk {i+1}: {resp.usage.input_tokens}in/{resp.usage.output_tokens}out "
              f"${cost:.5f} (total ${total_spent:.5f})")

    print(f"\nProcessed {len(results)}/{len(chunks)} chunks, ${total_spent:.5f} total")
    return results

def cost_report(doc_id: str):
    con = sqlite3.connect(DB)
    row = con.execute("""
        SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd)
        FROM chunk_runs WHERE doc_id=?
    """, (doc_id,)).fetchone()
    con.close()
    if row[0]:
        print(f"\nReport [{doc_id}]: {row[0]} chunks | "
              f"{row[1]}in + {row[2]}out tokens | ${row[3]:.5f}")

document = "Python is used for data science and web development. " * 400
results = process_with_budget(
    doc_id="doc-001",
    document=document,
    task="Summarize in one sentence:",
    token_budget_per_chunk=600,
    max_spend_usd=0.005,
)
cost_report("doc-001")

# Expected Token Savings: Budget cap hard-stops processing before overspend; pre-flight estimate avoids surprise bills
# Environment: adjust HAIKU_INPUT_COST/OUTPUT_COST if Anthropic updates pricing; SQLite tracks per-doc spend
```

## Comparison

| Option | Boundary Type | Overlap | Cost Tracking | Budget Cap |
|--------|-------------|---------|--------------|-----------|
| 1 — Fixed Character | Word boundary | Token-based | Per-run summary | No |
| 2 — Sentence Boundary | Sentence split | N sentences | Per-run summary | No |
| 3 — Map-Reduce | Paragraph | None | Map + reduce | No |
| 4 — Sliding Window | Fixed step | Configurable % | Per-window | No |
| 5 — Semantic Boundary | Headers/sections | None | Per-run summary | No |
| 6 — Budget-Enforced | Word boundary | None | SQLite per-chunk | Yes |
