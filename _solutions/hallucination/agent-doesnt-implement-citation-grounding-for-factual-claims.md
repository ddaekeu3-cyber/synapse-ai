---
layout: solution
title: "Agent Doesn't Implement Citation Grounding for Factual Claims"
category: hallucination
description: "Force the agent to ground every factual claim with a citation to provided source material, preventing hallucinated facts by requiring evidence before assertion."
tags: [citation, grounding, rag, hallucination, factual, retrieval-augmented]
---

# Agent Doesn't Implement Citation Grounding for Factual Claims

Agents freely state facts from training data without acknowledging when those facts may be outdated, incorrect, or entirely fabricated. Citation grounding requires the model to attribute every factual claim to a specific passage in the provided context — if no supporting passage exists, the agent must say so instead of inventing one.

## Option 1: Inline Citation with Source IDs

```python
import anthropic

client = anthropic.Anthropic()

CITATION_SYSTEM = """You are a research assistant that only states facts supported by the provided sources.

Rules:
1. Every factual claim MUST be followed by [Source N] referencing the source number.
2. If a fact is not in the provided sources, say "I don't have a source for that."
3. Never invent or infer facts not present in the sources.
4. Summarize only what the sources say, using [Source N] for each claim."""


def answer_with_citations(question: str, sources: list[str]) -> str:
    source_block = "\n\n".join(f"[Source {i+1}]: {src}" for i, src in enumerate(sources))
    prompt = f"""Sources:\n{source_block}\n\nQuestion: {question}\n\nAnswer with inline citations:"""

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=CITATION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


if __name__ == "__main__":
    sources = [
        "Python 3.11 introduced significant performance improvements, with the CPython interpreter running 10-60% faster than Python 3.10.",
        "The asyncio module was introduced in Python 3.4 as a library for writing concurrent code using coroutines.",
        "Python 3.12 added support for type parameter syntax (PEP 695) and improved error messages.",
    ]
    answer = answer_with_citations(
        "What performance improvements did Python 3.11 introduce?", sources
    )
    print(answer)

# Expected Token Savings: N/A (quality pattern); prevents hallucinated version numbers and dates
# Environment: Python 3.9+; source length determines token cost — chunk large docs before passing
```

## Option 2: Structured JSON Citations with Confidence Scores

```python
import json
import anthropic

client = anthropic.Anthropic()

STRUCTURED_CITATION_SYSTEM = """You are a fact-extraction assistant. Given sources and a question, extract only facts supported by the sources.

Return a JSON object with:
{
  "answer": "<concise answer in plain prose>",
  "claims": [
    {
      "claim": "<specific factual statement>",
      "source_id": <integer, 1-based>,
      "quote": "<exact quote from source supporting this claim>",
      "confidence": <float 0.0-1.0>
    }
  ],
  "unsupported_aspects": ["<aspects of the question not covered by provided sources>"]
}"""


def grounded_answer(question: str, sources: list[str]) -> dict:
    source_block = "\n\n".join(f"[{i+1}] {src}" for i, src in enumerate(sources))
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        system=STRUCTURED_CITATION_SYSTEM,
        messages=[{"role": "user", "content": f"Sources:\n{source_block}\n\nQuestion: {question}"}],
    )
    try:
        return json.loads(r.content[0].text)
    except json.JSONDecodeError:
        return {"answer": r.content[0].text, "claims": [], "unsupported_aspects": []}


def format_grounded_answer(data: dict) -> str:
    lines = [f"Answer: {data.get('answer', '')}\n"]
    for claim in data.get("claims", []):
        conf = claim.get("confidence", 0)
        lines.append(f"  • {claim['claim']}")
        lines.append(f"    → Source {claim.get('source_id', '?')}: \"{claim.get('quote', '')[:80]}\"")
        lines.append(f"    → Confidence: {conf:.0%}")
    unsupported = data.get("unsupported_aspects", [])
    if unsupported:
        lines.append(f"\nNot covered by sources: {', '.join(unsupported)}")
    return "\n".join(lines)


if __name__ == "__main__":
    sources = [
        "The Anthropic Claude API supports streaming responses via the messages.stream() method, which yields text deltas as they are generated.",
        "Claude models support a system prompt that sets the assistant's behavior and persona for the entire conversation.",
        "The maximum context window for Claude claude-opus-4-6 is 200,000 tokens as of 2024.",
    ]
    result = grounded_answer("How does Claude handle streaming and what is its context limit?", sources)
    print(format_grounded_answer(result))

# Expected Token Savings: JSON extraction adds ~100 tokens; structured citations enable automated verification
# Environment: Python 3.9+; parse claims array to auto-verify quotes against original sources
```

## Option 3: Post-Generation Citation Verification Pass

```python
import re
import anthropic

client = anthropic.Anthropic()

VERIFY_SYSTEM = """You are a citation auditor. Given a response with [Source N] citations and the original sources, verify each citation.

For each citation in the response:
1. Check that the referenced source actually supports the claim.
2. Mark it as VALID or INVALID.
3. If INVALID, provide the correct source ID or state "unsupported".

Return your audit as:
CITATION [Source N] in claim "<claim text>": VALID / INVALID (reason)"""


def generate_with_citations(question: str, sources: list[str]) -> str:
    source_block = "\n\n".join(f"[Source {i+1}]: {src}" for i, src in enumerate(sources))
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="Answer with [Source N] citations for every factual claim. Use only provided sources.",
        messages=[{"role": "user", "content": f"Sources:\n{source_block}\n\nQuestion: {question}"}],
    )
    return r.content[0].text


def verify_citations(response: str, sources: list[str]) -> tuple[str, list[dict]]:
    source_block = "\n\n".join(f"[Source {i+1}]: {src}" for i, src in enumerate(sources))
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=VERIFY_SYSTEM,
        messages=[{"role": "user", "content": f"Sources:\n{source_block}\n\nResponse to audit:\n{response}"}],
    )
    audit_text = r.content[0].text

    # Parse audit results
    results = []
    for line in audit_text.split("\n"):
        if "VALID" in line or "INVALID" in line:
            valid = "INVALID" not in line
            results.append({"line": line.strip(), "valid": valid})

    return audit_text, results


def answered_and_verified(question: str, sources: list[str]) -> str:
    response = generate_with_citations(question, sources)
    print(f"[DRAFT]\n{response}\n")

    audit, results = verify_citations(response, sources)
    invalid = [r for r in results if not r["valid"]]

    if invalid:
        print(f"[AUDIT] ⚠ {len(invalid)} invalid citation(s) found:")
        for r in invalid:
            print(f"  {r['line']}")
        # Regenerate with audit feedback
        r2 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="Answer with [Source N] citations. Fix all citation issues noted.",
            messages=[
                {"role": "user", "content": f"Sources:\n{chr(10).join(f'[Source {i+1}]: {s}' for i,s in enumerate(sources))}\n\nQuestion: {question}"},
                {"role": "assistant", "content": response},
                {"role": "user", "content": f"Citation audit found issues:\n{audit}\n\nPlease rewrite with corrected citations."},
            ],
        )
        return r2.content[0].text
    else:
        print("[AUDIT] ✓ All citations valid")
        return response


if __name__ == "__main__":
    sources = [
        "Redis supports five main data types: strings, hashes, lists, sets, and sorted sets.",
        "Redis Streams were added in Redis 5.0 and provide a log-like data structure for message queuing.",
        "Redis persistence can be configured with RDB snapshots, AOF logging, or both.",
    ]
    print(answered_and_verified("What data types and persistence options does Redis support?", sources))

# Expected Token Savings: Verification adds ~300 tokens but catches hallucinated citations before delivery
# Environment: Python 3.9+; use for high-stakes factual Q&A (legal, medical, technical documentation)
```

## Option 4: RAG Pipeline with Source Chunk Attribution

```python
import re
import math
import anthropic
from collections import Counter
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class Chunk:
    id: str
    text: str
    source_file: str
    page: int = 0


def tokenize(text: str) -> Counter:
    return Counter(re.findall(r'\w+', text.lower()))


def bm25_score(query: str, chunk: Chunk, k1: float = 1.5, b: float = 0.75, avg_len: float = 100) -> float:
    q_tokens = tokenize(query)
    c_tokens = tokenize(chunk.text)
    doc_len = sum(c_tokens.values())
    score = 0.0
    for term, q_freq in q_tokens.items():
        tf = c_tokens.get(term, 0)
        if tf == 0:
            continue
        idf = math.log(1 + 1 / (tf + 0.5))
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_len))
        score += idf * tf_norm
    return score


CORPUS: list[Chunk] = [
    Chunk("c1", "Python asyncio uses an event loop to schedule and run coroutines. Tasks are scheduled with asyncio.create_task().", "python-docs.txt", 1),
    Chunk("c2", "asyncio.gather() runs multiple coroutines concurrently and returns their results as a list.", "python-docs.txt", 2),
    Chunk("c3", "The asyncio.Queue class provides a FIFO queue for producer-consumer patterns in async code.", "python-docs.txt", 3),
    Chunk("c4", "asyncio.Semaphore limits the number of coroutines that can execute a section of code concurrently.", "python-docs.txt", 4),
    Chunk("c5", "Exception handling in asyncio: use try/except inside coroutines. Unhandled exceptions in Tasks are logged but not raised until awaited.", "python-docs.txt", 5),
]

GROUNDED_SYSTEM = """You are a technical assistant. Answer ONLY using the provided document chunks.
For each statement, include the chunk ID in brackets, e.g. [c1].
If the answer is not in the chunks, say: "The provided documents do not contain this information." """


def rag_with_citations(question: str, top_k: int = 3) -> str:
    # Retrieve top-k chunks
    scored = sorted(CORPUS, key=lambda c: bm25_score(question, c), reverse=True)[:top_k]

    chunk_block = "\n\n".join(
        f"[{c.id}] (from {c.source_file}, p.{c.page}):\n{c.text}"
        for c in scored
    )
    print(f"[RAG] Retrieved: {[c.id for c in scored]}")

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=GROUNDED_SYSTEM,
        messages=[{"role": "user", "content": f"Document chunks:\n{chunk_block}\n\nQuestion: {question}"}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(rag_with_citations("How do I run multiple coroutines concurrently and limit parallelism?"))
    print(rag_with_citations("How does asyncio handle exceptions in background tasks?"))

# Expected Token Savings: Top-3 BM25 retrieval sends ~300 tokens vs. full corpus; citations enable tracing
# Environment: Python 3.9+; replace BM25 with vector search for semantic retrieval at scale
```

## Option 5: Claim Extraction + Per-Claim Source Matching

```python
import json
import anthropic

client = anthropic.Anthropic()

EXTRACT_CLAIMS_PROMPT = """Extract all factual claims from the following text as a JSON array of strings.
Each claim should be a single, atomic, verifiable statement.
Return only the JSON array.

Text: {text}"""

MATCH_SOURCE_PROMPT = """Given a factual claim and a list of sources, find which source (if any) supports the claim.

Claim: {claim}

Sources:
{sources}

Return JSON: {{"source_id": <integer 1-based or null>, "quote": "<supporting quote or null>", "supported": <true/false>}}"""


def extract_claims(text: str) -> list[str]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": EXTRACT_CLAIMS_PROMPT.format(text=text)}],
    )
    try:
        return json.loads(r.content[0].text)
    except json.JSONDecodeError:
        return []


def match_claim_to_source(claim: str, sources: list[str]) -> dict:
    source_block = "\n".join(f"{i+1}. {s}" for i, s in enumerate(sources))
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": MATCH_SOURCE_PROMPT.format(
            claim=claim, sources=source_block
        )}],
    )
    try:
        return json.loads(r.content[0].text)
    except json.JSONDecodeError:
        return {"source_id": None, "quote": None, "supported": False}


def verify_response(response: str, sources: list[str]) -> dict:
    claims = extract_claims(response)
    results = []
    for claim in claims:
        match = match_claim_to_source(claim, sources)
        results.append({
            "claim": claim,
            "supported": match["supported"],
            "source_id": match.get("source_id"),
            "quote": match.get("quote"),
        })
    unsupported = [r for r in results if not r["supported"]]
    return {
        "total_claims": len(claims),
        "supported": len(results) - len(unsupported),
        "unsupported": unsupported,
        "grounding_rate": (len(results) - len(unsupported)) / len(results) if results else 1.0,
    }


def run_grounded_agent(question: str, sources: list[str]) -> tuple[str, dict]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Sources:\n{chr(10).join(f'{i+1}. {s}' for i,s in enumerate(sources))}\n\nAnswer: {question}",
        }],
    )
    response = r.content[0].text
    verification = verify_response(response, sources)
    return response, verification


if __name__ == "__main__":
    sources = [
        "SQLite supports ACID transactions and uses a single file for the entire database.",
        "SQLite does not support stored procedures or user-defined functions natively.",
        "SQLite's WAL (Write-Ahead Logging) mode allows concurrent reads during writes.",
    ]
    response, verification = run_grounded_agent("What are SQLite's key features and limitations?", sources)
    print(response)
    print(f"\n[GROUNDING] {verification['supported']}/{verification['total_claims']} claims supported "
          f"({verification['grounding_rate']:.0%})")
    for u in verification["unsupported"]:
        print(f"  ⚠ Unsupported: {u['claim']}")

# Expected Token Savings: Per-claim verification adds ~80 tokens/claim; enables grounding score metric
# Environment: Python 3.9+; grounding_rate < 0.8 triggers regeneration or user warning
```

## Option 6: Multi-Source Citation with Conflict Detection

```python
import json
import anthropic

client = anthropic.Anthropic()

CONFLICT_DETECT_SYSTEM = """You are a fact-checking assistant with access to multiple sources that may contradict each other.

When answering:
1. Cite each claim with [Source N].
2. If sources CONFLICT on a claim, explicitly note: "Sources conflict: [Source A] says X, [Source B] says Y."
3. If a claim appears in only one source, note it as a single-source claim.
4. Conclude with a CONFIDENCE rating: HIGH (all sources agree), MEDIUM (partial support), LOW (sources conflict or single-source)."""


def multi_source_answer(question: str, sources: dict[str, str]) -> str:
    source_block = "\n\n".join(f"[Source {name}]: {text}" for name, text in sources.items())
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        system=CONFLICT_DETECT_SYSTEM,
        messages=[{"role": "user", "content": f"Sources:\n{source_block}\n\nQuestion: {question}"}],
    )
    return r.content[0].text


if __name__ == "__main__":
    # Intentionally conflicting sources to test conflict detection
    sources = {
        "OpenAI-docs-2023": "GPT-4 has a context window of 8,192 tokens in the standard version.",
        "OpenAI-blog-2024": "GPT-4 Turbo supports up to 128,000 tokens in its context window.",
        "TechReview-2024": "GPT-4o offers a 128K context window with improved speed compared to GPT-4 Turbo.",
    }
    answer = multi_source_answer("What is the context window size of GPT-4?", sources)
    print(answer)

# Expected Token Savings: Conflict detection prevents false consensus; single LLM call handles multiple sources
# Environment: Python 3.9+; use for research synthesis, competitive analysis, or multi-vendor comparisons
```

## Comparison

| Option | Citation Style | Verification | Conflict Detection | Unsupported Handling | Best For |
|--------|---------------|-------------|-------------------|---------------------|----------|
| 1. Inline [Source N] | Inline markers | No | No | Explicit admission | Simple Q&A |
| 2. Structured JSON | JSON claims array | No | No | unsupported_aspects field | Automated pipelines |
| 3. Post-Gen Verify | Audit pass | Yes | No | Regeneration | High-stakes answers |
| 4. RAG + Chunk ID | Chunk ID markers | No | No | "Not in docs" | Document retrieval |
| 5. Claim Extraction | Per-claim match | Yes | No | Grounding rate metric | Hallucination auditing |
| 6. Multi-Source | Conflict notes | Implicit | Yes | CONFIDENCE rating | Multi-source synthesis |
