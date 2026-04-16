---
title: "Agent Doesn't Implement Prompt Compression with Entropy Filtering"
description: "Agents pass raw, verbose tool outputs and conversation history directly into prompts, wasting tokens on low-information content that increases cost and latency without improving response quality."
difficulty: intermediate
category: performance
tags: [prompt-compression, token-efficiency, entropy, summarization, llm, performance, cost]
---

## Problem

Tool outputs, retrieved documents, and long conversation histories often contain far more tokens than the information they carry. Boilerplate headers, repeated phrases, verbose JSON field names, and redundant sentences all inflate the context window without improving model output quality. An agent that injects raw content pays per-token costs for noise.

```python
# Broken: raw 8KB tool output injected verbatim
async def call_model(tool_result: str, question: str):
    # tool_result may be 2000 tokens of verbose JSON with 50 tokens of signal
    messages = [
        {"role": "user", "content": f"Tool result:\n{tool_result}\n\nQuestion: {question}"}
    ]
    response = await client.messages.create(
        model="claude-opus-4-6", max_tokens=1024, messages=messages
    )
    return response.content[0].text
```

---

## Solution 1: Whitespace and Repetition Normalization

```python
import re

def normalize_whitespace(text: str) -> str:
    """Remove excessive blank lines, trailing spaces, and normalize indentation."""
    # Collapse 3+ newlines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Strip trailing whitespace on each line
    text = '\n'.join(line.rstrip() for line in text.splitlines())
    # Collapse multiple spaces (outside code blocks)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()

def remove_repeated_lines(text: str, min_length: int = 20) -> str:
    """Remove duplicate lines (exact match), keeping first occurrence."""
    seen: set[str] = set()
    result: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) >= min_length and stripped in seen:
            continue
        seen.add(stripped)
        result.append(line)
    return '\n'.join(result)

def remove_boilerplate_headers(text: str) -> str:
    """Strip common API response boilerplate."""
    boilerplate_patterns = [
        r'^HTTP/\d\.\d \d+ .+$',
        r'^(Content-Type|Content-Length|Date|Server|X-Request-Id): .+$',
        r'^={3,}$',
        r'^-{3,}$',
        r'^\s*"status"\s*:\s*"(ok|success|200)"\s*,?\s*$',
    ]
    combined = re.compile('|'.join(boilerplate_patterns), re.MULTILINE)
    return combined.sub('', text)

def basic_compress(text: str) -> str:
    text = normalize_whitespace(text)
    text = remove_repeated_lines(text)
    text = remove_boilerplate_headers(text)
    return normalize_whitespace(text)

# Before: 3,200 chars → After: ~2,100 chars on typical API responses
```

---

## Solution 2: Stop-Word and Low-Information Token Removal from Tool Outputs

```python
import json
import re
from typing import Any

# JSON keys that are almost always low-information in API responses
LOW_VALUE_JSON_KEYS = frozenset({
    "_links", "_embedded", "links", "href", "self",
    "created_at", "updated_at", "deleted_at",
    "etag", "last_modified", "cache_control",
    "x_request_id", "x_trace_id", "request_id",
    "pagination", "page", "per_page", "total_pages",
    "null", "metadata", "_metadata",
})

def prune_json_keys(obj: Any, drop_keys: frozenset[str] = LOW_VALUE_JSON_KEYS,
                    max_depth: int = 5, depth: int = 0) -> Any:
    """Recursively remove low-value keys from JSON objects."""
    if depth > max_depth:
        return obj
    if isinstance(obj, dict):
        return {
            k: prune_json_keys(v, drop_keys, max_depth, depth + 1)
            for k, v in obj.items()
            if k not in drop_keys and v is not None and v != "" and v != []
        }
    if isinstance(obj, list):
        return [prune_json_keys(item, drop_keys, max_depth, depth + 1)
                for item in obj]
    return obj

def compress_json_tool_output(raw: str,
                              max_array_items: int = 5) -> str:
    """
    Parse JSON tool output, prune low-value keys, truncate long arrays,
    and re-serialize compactly.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return basic_compress(raw)  # fall back to text compression

    obj = prune_json_keys(obj)
    obj = _truncate_arrays(obj, max_array_items)
    # Compact serialization (no extra whitespace)
    return json.dumps(obj, separators=(',', ':'))

def _truncate_arrays(obj: Any, max_items: int) -> Any:
    if isinstance(obj, list) and len(obj) > max_items:
        truncated = [_truncate_arrays(item, max_items) for item in obj[:max_items]]
        truncated.append(f"... ({len(obj) - max_items} more items)")
        return truncated
    if isinstance(obj, dict):
        return {k: _truncate_arrays(v, max_items) for k, v in obj.items()}
    return obj

def count_tokens_approx(text: str) -> int:
    """Approximate token count: ~4 chars per token for English text."""
    return max(1, len(text) // 4)

def compression_ratio(original: str, compressed: str) -> float:
    orig_tokens = count_tokens_approx(original)
    comp_tokens = count_tokens_approx(compressed)
    return 1.0 - (comp_tokens / orig_tokens)
```

---

## Solution 3: Redundant Sentence Deduplication with Semantic Hashing

```python
import hashlib
import re
from collections import Counter

def sentence_tokenize(text: str) -> list[str]:
    """Simple sentence splitter (no external deps)."""
    # Split on sentence-ending punctuation followed by space+capital
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    return [s.strip() for s in sentences if s.strip()]

def normalize_sentence(s: str) -> str:
    """Normalize for deduplication: lowercase, strip punctuation, collapse spaces."""
    s = s.lower()
    s = re.sub(r'[^\w\s]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def sentence_fingerprint(s: str) -> str:
    return hashlib.md5(normalize_sentence(s).encode()).hexdigest()[:8]

def deduplicate_sentences(text: str,
                          similarity_threshold: float = 0.85) -> str:
    """
    Remove near-duplicate sentences using character n-gram overlap.
    Keeps the first occurrence of each near-duplicate cluster.
    """
    sentences = sentence_tokenize(text)
    kept: list[str] = []
    kept_normalized: list[str] = []

    for sentence in sentences:
        norm = normalize_sentence(sentence)
        if len(norm) < 20:  # too short to deduplicate
            kept.append(sentence)
            continue

        # Check n-gram overlap with all kept sentences
        is_duplicate = False
        for existing_norm in kept_normalized:
            overlap = _ngram_overlap(norm, existing_norm, n=3)
            if overlap >= similarity_threshold:
                is_duplicate = True
                break

        if not is_duplicate:
            kept.append(sentence)
            kept_normalized.append(norm)

    return ' '.join(kept)

def _ngram_overlap(a: str, b: str, n: int = 3) -> float:
    """Jaccard similarity on character n-grams."""
    def ngrams(s: str) -> set[str]:
        return {s[i:i+n] for i in range(len(s) - n + 1)}

    a_ng, b_ng = ngrams(a), ngrams(b)
    if not a_ng and not b_ng:
        return 1.0
    intersection = len(a_ng & b_ng)
    union = len(a_ng | b_ng)
    return intersection / union if union > 0 else 0.0

def compress_conversation_history(turns: list[dict],
                                  max_turns: int = 20) -> list[dict]:
    """
    Compress old turns by deduplicating sentences and normalizing.
    Keep last `max_turns` turns at full fidelity.
    """
    if len(turns) <= max_turns:
        return turns

    compressed = []
    for i, turn in enumerate(turns):
        age = len(turns) - i
        content = turn.get("content", "")
        if isinstance(content, str):
            if age > max_turns:
                content = deduplicate_sentences(basic_compress(content))
                content = content[:500] + "..." if len(content) > 500 else content
        compressed.append({**turn, "content": content})

    return compressed
```

---

## Solution 4: Extractive Summarization via Sentence Scoring

```python
import math
import re
from collections import Counter

def score_sentences(text: str) -> list[tuple[str, float]]:
    """
    Score sentences by TF-IDF-like importance: prefer sentences with
    high-frequency content words and penalize short/generic sentences.
    """
    sentences = sentence_tokenize(text)
    if not sentences:
        return []

    # Build word frequency table (content words only)
    all_words: list[str] = []
    for s in sentences:
        words = [w.lower() for w in re.findall(r'\b[a-zA-Z]{3,}\b', s)
                 if w.lower() not in _STOPWORDS]
        all_words.extend(words)

    word_freq = Counter(all_words)
    max_freq = max(word_freq.values()) if word_freq else 1

    scored: list[tuple[str, float]] = []
    for sent in sentences:
        words = [w.lower() for w in re.findall(r'\b[a-zA-Z]{3,}\b', sent)
                 if w.lower() not in _STOPWORDS]
        if not words:
            scored.append((sent, 0.0))
            continue
        # Normalized TF score
        tf_score = sum(word_freq[w] / max_freq for w in words) / len(words)
        # Penalize very short sentences (< 8 words)
        length_bonus = min(1.0, len(words) / 8)
        scored.append((sent, tf_score * length_bonus))

    return scored

def extractive_compress(text: str, keep_ratio: float = 0.4) -> str:
    """Keep the top-scoring sentences by TF importance."""
    scored = score_sentences(text)
    if not scored:
        return text

    n_keep = max(1, int(len(scored) * keep_ratio))
    top_indices = sorted(
        range(len(scored)),
        key=lambda i: scored[i][1],
        reverse=True
    )[:n_keep]

    # Preserve original order
    kept_indices = sorted(top_indices)
    return ' '.join(scored[i][0] for i in kept_indices)

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
    "for", "of", "with", "by", "from", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall",
    "this", "that", "these", "those", "it", "its", "as", "if", "so",
})

def token_budget_extract(text: str, token_budget: int) -> str:
    """Extract until token budget is exhausted, prioritizing high-score sentences."""
    scored = score_sentences(text)
    scored_with_idx = list(enumerate(scored))
    scored_with_idx.sort(key=lambda x: x[1][1], reverse=True)

    selected: list[tuple[int, str]] = []
    tokens_used = 0
    for idx, (sentence, score) in scored_with_idx:
        t = count_tokens_approx(sentence)
        if tokens_used + t > token_budget:
            continue
        selected.append((idx, sentence))
        tokens_used += t

    selected.sort(key=lambda x: x[0])  # restore order
    return ' '.join(s for _, s in selected)
```

---

## Solution 5: LLM-Based Prompt Compression (LLMLingua-Style)

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

COMPRESSION_SYSTEM = """You are a prompt compression specialist.
Your task: compress the provided text to fit within the token budget while
preserving ALL information necessary to answer the question.
Rules:
- Remove filler, boilerplate, repeated information
- Preserve numbers, names, dates, error messages exactly
- Use abbreviations for repeated long phrases (define first use)
- Output ONLY the compressed text, no commentary
"""

async def llm_compress(text: str, question: str, target_tokens: int) -> str:
    """
    Use Claude Haiku (fast, cheap) to compress context before sending to a
    more expensive model.
    """
    prompt = (
        f"Target token budget: {target_tokens} tokens\n"
        f"Question to answer: {question}\n\n"
        f"Text to compress:\n{text}"
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=target_tokens + 100,  # small headroom
        system=COMPRESSION_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

async def compress_then_answer(raw_context: str, question: str,
                               context_budget: int = 2000) -> str:
    """
    Two-stage pipeline:
    1. Haiku compresses the raw context
    2. Main model answers with compressed context
    """
    original_tokens = count_tokens_approx(raw_context)

    if original_tokens <= context_budget:
        # No compression needed
        compressed = raw_context
    else:
        print(f"[Compress] {original_tokens}→{context_budget} tokens "
              f"({100*(1-context_budget/original_tokens):.0f}% reduction)")
        compressed = await llm_compress(raw_context, question, context_budget)

    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Context:\n{compressed}\n\nQuestion: {question}"
        }]
    )
    return response.content[0].text

# Batch compression for multiple tool results
async def compress_tool_results(results: list[dict],
                                question: str,
                                total_budget: int = 4000) -> list[dict]:
    """Allocate token budget proportionally across tool results."""
    if not results:
        return results

    # Measure each result
    sizes = [count_tokens_approx(str(r.get("content", ""))) for r in results]
    total = sum(sizes)

    if total <= total_budget:
        return results

    # Proportional allocation
    compressed = []
    tasks = []
    for result, size in zip(results, sizes):
        alloc = max(100, int(total_budget * size / total))
        content = str(result.get("content", ""))
        tasks.append(llm_compress(content, question, alloc))

    compressed_contents = await asyncio.gather(*tasks)
    return [
        {**r, "content": c}
        for r, c in zip(results, compressed_contents)
    ]
```

---

## Solution 6: Token-Budget-Aware Hierarchical Compression Pipeline

```python
import asyncio
from dataclasses import dataclass
from typing import Callable

@dataclass
class CompressionResult:
    text: str
    original_tokens: int
    compressed_tokens: int
    strategy_used: str

    @property
    def ratio(self) -> float:
        return 1.0 - (self.compressed_tokens / max(1, self.original_tokens))

class HierarchicalCompressor:
    """
    Apply compression strategies in order of cost/aggressiveness:
    1. Normalization (free, always)
    2. JSON pruning (free, if JSON)
    3. Sentence dedup (cheap)
    4. Extractive compression (cheap)
    5. LLM compression (expensive, last resort)
    """

    def __init__(self, llm_compress_fn: Callable | None = None):
        self._llm_compress = llm_compress_fn

    async def compress(self, text: str, token_budget: int,
                       question: str = "") -> CompressionResult:
        original = count_tokens_approx(text)

        # Stage 1: always normalize
        result = basic_compress(text)
        if count_tokens_approx(result) <= token_budget:
            return CompressionResult(result, original,
                                     count_tokens_approx(result), "normalize")

        # Stage 2: JSON pruning
        if text.lstrip().startswith('{') or text.lstrip().startswith('['):
            result = compress_json_tool_output(text)
            if count_tokens_approx(result) <= token_budget:
                return CompressionResult(result, original,
                                         count_tokens_approx(result), "json_prune")

        # Stage 3: sentence deduplication
        result = deduplicate_sentences(result)
        if count_tokens_approx(result) <= token_budget:
            return CompressionResult(result, original,
                                     count_tokens_approx(result), "dedup")

        # Stage 4: extractive
        result = token_budget_extract(result, token_budget)
        if count_tokens_approx(result) <= token_budget:
            return CompressionResult(result, original,
                                     count_tokens_approx(result), "extractive")

        # Stage 5: LLM compression (only if provided and still over budget)
        if self._llm_compress and question:
            result = await self._llm_compress(text, question, token_budget)
            return CompressionResult(result, original,
                                     count_tokens_approx(result), "llm")

        # Hard truncation as last resort
        words = result.split()
        result = ' '.join(words[:token_budget * 4 // 5])  # ~80% headroom
        return CompressionResult(result + " [TRUNCATED]", original,
                                 count_tokens_approx(result), "truncate")

    async def compress_many(self, texts: list[str],
                            total_budget: int,
                            question: str = "") -> list[CompressionResult]:
        """Compress a list of texts sharing a total budget."""
        sizes = [count_tokens_approx(t) for t in texts]
        total = sum(sizes)
        results = []
        tasks = []
        for text, size in zip(texts, sizes):
            alloc = max(50, int(total_budget * size / total))
            tasks.append(self.compress(text, alloc, question))
        return await asyncio.gather(*tasks)

# Usage
async def demo():
    compressor = HierarchicalCompressor(llm_compress_fn=llm_compress)

    raw_tool_output = """
    {
      "status": "ok",
      "request_id": "req-abc-123",
      "_links": {"self": {"href": "/api/v1/results/123"}},
      "data": {
        "items": [
          {"id": 1, "name": "Result A", "score": 0.95, "created_at": "2024-01-01"},
          {"id": 2, "name": "Result B", "score": 0.87, "created_at": "2024-01-02"},
          {"id": 3, "name": "Result C", "score": 0.76, "created_at": "2024-01-03"}
        ],
        "total": 3,
        "pagination": {"page": 1, "per_page": 10, "total_pages": 1}
      },
      "metadata": {"version": "1.0", "generated_at": "2024-01-10T12:00:00Z"}
    }
    """ * 10  # simulate verbose output

    cr = await compressor.compress(raw_tool_output, token_budget=200,
                                    question="What are the top scores?")
    print(f"Strategy: {cr.strategy_used}, "
          f"Reduction: {cr.ratio:.0%} "
          f"({cr.original_tokens}→{cr.compressed_tokens} tokens)")
```

---

## Comparison

| Solution | Speed | Cost | Quality | Handles JSON | Handles Prose | Best For |
|---|---|---|---|---|---|---|
| 1. Normalize + dedup lines | Instant | Free | Basic | Partial | Yes | Always-on pre-processing |
| 2. JSON key pruning | Instant | Free | Good | Yes | No | Structured tool outputs |
| 3. Sentence dedup | Fast | Free | Good | No | Yes | Conversation history |
| 4. Extractive scoring | Fast | Free | Good | No | Yes | Long documents |
| 5. LLM compression | Slow | Low (Haiku) | Excellent | Yes | Yes | High-value contexts |
| 6. Hierarchical pipeline | Adaptive | Adaptive | Excellent | Yes | Yes | Production agents |

**Key principle**: apply cheap strategies first and only escalate to LLM compression when necessary. A typical JSON tool output drops 40–70% of tokens with free strategies alone. LLM compression is reserved for prose-heavy content that doesn't compress well statistically.
