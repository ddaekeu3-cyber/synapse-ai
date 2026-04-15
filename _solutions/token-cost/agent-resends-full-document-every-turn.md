---
layout: solution
title: "Agent Resends Full Document Every Turn — Redundant Context Costing Millions of Tokens"
category: token-cost
description: "Agent is given a 50-page document. Every turn, the full document is included in the API call. 100 questions about the document = 100 × 50 pages sent to the API. Should use prompt caching."
tags: [token-cost, prompt-caching, context, document, redundant, expensive]
---

# Agent Resends Full Document Every Turn — Redundant Context Costing Millions of Tokens

## Symptom
- Processing 100 questions about a 50-page report costs 10M+ tokens
- API costs are 10–50× higher than expected
- Every response takes the same 8–12 seconds regardless of question complexity
- Large document or codebase included verbatim in every message
- Token counter shows constant 80K+ input tokens per call

## Root Cause
Without prompt caching, every API call re-sends all input tokens including stable content (documents, system prompts, codebase context). If the same 50,000-token document appears in 100 API calls, that's 5,000,000 input tokens charged at full price. Prompt caching can reduce the cost of repeated stable content by ~90%.

## Fix

### Option 1: Use Anthropic prompt caching (cache_control)

```python
import anthropic

client = anthropic.Anthropic()

def ask_about_document(document: str, question: str) -> str:
    """Cache the document — only charge for it once per 5 minutes"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": "You are a document analysis assistant.",
            },
            {
                "type": "text",
                "text": document,
                "cache_control": {"type": "ephemeral"}  # Cache this content
            }
        ],
        messages=[
            {"role": "user", "content": question}
        ]
    )

    usage = response.usage
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Cache read tokens: {usage.cache_read_input_tokens}")   # Cached — cheap
    print(f"Cache write tokens: {usage.cache_creation_input_tokens}")  # First time — normal price

    return response.content[0].text

# First call: full document charged (cache write)
answer_1 = ask_about_document(large_document, "What is the main conclusion?")

# Subsequent calls within 5 minutes: document served from cache (~10% cost)
answer_2 = ask_about_document(large_document, "What are the limitations?")
answer_3 = ask_about_document(large_document, "List all recommendations.")
```

### Option 2: Cache large system prompts

```python
# Expensive: resend 10K system prompt every call
response = client.messages.create(
    model="claude-sonnet-4-6",
    system=long_system_prompt,  # Resent every call — no caching
    messages=[{"role": "user", "content": question}]
)

# Cheap: cache the system prompt
response = client.messages.create(
    model="claude-sonnet-4-6",
    system=[{
        "type": "text",
        "text": long_system_prompt,
        "cache_control": {"type": "ephemeral"}  # Cache for 5 minutes
    }],
    messages=[{"role": "user", "content": question}]
)
```

### Option 3: Split stable context from dynamic context

```python
class CachedDocumentAgent:
    def __init__(self, stable_document: str):
        self.stable_document = stable_document
        self.conversation_history = []

    def ask(self, question: str) -> str:
        """Only the question changes — document is cached"""
        self.conversation_history.append({"role": "user", "content": question})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": "Answer questions about the following document:",
                },
                {
                    "type": "text",
                    "text": self.stable_document,
                    "cache_control": {"type": "ephemeral"}  # Cached — stable
                }
            ],
            messages=self.conversation_history  # Dynamic — not cached
        )

        answer = response.content[0].text
        self.conversation_history.append({"role": "assistant", "content": answer})
        return answer

# Usage: 1000 questions about the same document
agent = CachedDocumentAgent(large_pdf_text)
for question in questions:
    print(agent.ask(question))
# Document charged once per 5-min cache window, not 1000 times
```

### Option 4: RAG — retrieve only relevant chunks

```python
from sentence_transformers import SentenceTransformer
import numpy as np

class SimpleRAG:
    def __init__(self, document: str, chunk_size: int = 500):
        self.model = SentenceTransformer("all-MiniLM-L6-v2")
        self.chunks = self._chunk(document, chunk_size)
        self.embeddings = self.model.encode(self.chunks)

    def _chunk(self, text: str, size: int) -> list[str]:
        words = text.split()
        return [" ".join(words[i:i+size]) for i in range(0, len(words), size)]

    def retrieve(self, query: str, top_k: int = 3) -> str:
        query_emb = self.model.encode([query])
        scores = np.dot(self.embeddings, query_emb.T).flatten()
        top_indices = scores.argsort()[-top_k:][::-1]
        return "\n\n---\n\n".join(self.chunks[i] for i in top_indices)

rag = SimpleRAG(large_document)

def ask_with_rag(question: str) -> str:
    # Only send relevant chunks, not full document
    relevant_context = rag.retrieve(question, top_k=3)  # ~1500 words
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Context:\n{relevant_context}\n\nQuestion: {question}"
        }]
    )
    return response.content[0].text
```

### Option 5: Monitor cache hit rate

```python
class CacheAwareClient:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.total_input = 0
        self.total_cache_read = 0
        self.total_cache_write = 0

    def create(self, **kwargs):
        response = self.client.messages.create(**kwargs)
        usage = response.usage
        self.total_input += usage.input_tokens
        self.total_cache_read += getattr(usage, "cache_read_input_tokens", 0)
        self.total_cache_write += getattr(usage, "cache_creation_input_tokens", 0)
        return response

    def report(self):
        total = self.total_input + self.total_cache_read + self.total_cache_write
        if total > 0:
            cache_hit_rate = self.total_cache_read / total
            print(f"Cache hit rate: {cache_hit_rate:.0%}")
            print(f"Tokens charged at full price: {self.total_input + self.total_cache_write:,}")
            print(f"Tokens served from cache: {self.total_cache_read:,}")
```

## Cost Comparison (claude-sonnet-4-6)

| Approach | 100 questions × 50K doc | Cost estimate |
|---|---|---|
| No caching | 5,000,000 input tokens | ~$15 |
| Prompt caching | 50K write + 4,950K cache read | ~$2 |
| RAG (3 chunks) | 100 × ~1,500 = 150K tokens | ~$0.45 |

*Approximate. Check current Anthropic pricing at anthropic.com/pricing*

## Expected Token Savings
100 Q&A without caching: 5,000,000 tokens
100 Q&A with prompt caching: ~200,000 effective tokens (96% reduction)

## Environment
- Agents doing Q&A over large documents, code review of entire codebases
- Source: direct measurement, Anthropic prompt caching documentation
