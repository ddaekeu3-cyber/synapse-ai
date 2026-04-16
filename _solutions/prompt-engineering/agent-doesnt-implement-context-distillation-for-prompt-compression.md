---
layout: solution
title: "Agent Doesn't Implement Context Distillation for Prompt Compression"
category: prompt-engineering
description: "Compress verbose context into dense, high-signal summaries before injecting into prompts, preserving key facts while dramatically reducing token usage."
tags: [prompt-engineering, compression, distillation, context, token-cost, summarization]
---

# Agent Doesn't Implement Context Distillation for Prompt Compression

Long documents, conversation histories, and tool results bloat prompts with redundant tokens. Simply truncating context loses critical information. Context distillation compresses verbose input into dense, high-signal summaries that preserve what matters — key facts, decisions, entities, and relationships — while cutting token count by 50-80%. The model then operates on the distilled essence rather than the full verbose original.

## Option 1: Extractive Distillation — Key Sentences Only

```python
import anthropic
import re

client = anthropic.Anthropic()


def extractive_distill(text: str, max_sentences: int = 5) -> str:
    """
    Select the most informative sentences from text.
    Uses sentence scoring based on keyword density and position.
    """
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sentences) <= max_sentences:
        return text

    # Score each sentence: position bonus + length normalization
    def score(i: int, sent: str) -> float:
        position_score = 1.0 if i == 0 else (0.5 if i == len(sentences) - 1 else 0.3)
        # Reward sentences with numbers, proper nouns, and key terms
        info_score = (
            len(re.findall(r'\d+', sent)) * 0.2 +
            len(re.findall(r'[A-Z][a-z]+', sent)) * 0.1 +
            min(len(sent.split()), 30) / 30 * 0.5
        )
        return position_score + info_score

    scored = [(score(i, s), i, s) for i, s in enumerate(sentences)]
    top = sorted(scored, reverse=True)[:max_sentences]
    # Restore original order
    selected = [s for _, _, s in sorted(top, key=lambda x: x[1])]
    return " ".join(selected)


def run_agent_with_extractive_distillation(verbose_context: str, question: str) -> str:
    original_tokens = len(verbose_context) // 4
    distilled = extractive_distill(verbose_context, max_sentences=5)
    distilled_tokens = len(distilled) // 4

    print(f"[distill] {original_tokens} -> {distilled_tokens} estimated tokens ({distilled_tokens/original_tokens:.0%} of original)")
    print(f"[distilled] {distilled[:200]}...")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Context:\n{distilled}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


verbose_doc = """
Python was created by Guido van Rossum and first released in 1991. The language design philosophy emphasizes code readability,
with its notable use of significant indentation. Python is dynamically typed and garbage-collected. It supports multiple
programming paradigms, including structured (particularly procedural), object-oriented and functional programming.
Python is often described as a 'batteries included' language due to its comprehensive standard library.
Python consistently ranks in surveys as one of the most popular programming languages. As of 2023, Python 3 is the
dominant version. Python 2 was officially discontinued on January 1, 2020. The CPython reference implementation is
written in C. Other implementations include PyPy, Jython, and IronPython. The name Python comes not from the snake
but from the British comedy group Monty Python. Python Software Foundation maintains the language. PEP 8 is the
style guide for Python code. Python uses indentation for block delimiters rather than curly braces.
Python has a large standard library sometimes characterized as 'batteries included'. The language features a dynamic
type system and automatic memory management. Python supports multiple programming paradigms.
"""

result = run_agent_with_extractive_distillation(verbose_doc, "When was Python created and who made it?")
print(f"\nAnswer: {result}")

# Expected Token Savings: 60-80% on verbose documents; extractive distillation preserves factual accuracy
# Environment: Python 3.11+; increase max_sentences for complex questions requiring more context
```

## Option 2: LLM-Powered Abstractive Distillation

```python
import anthropic

client = anthropic.Anthropic()

TARGET_WORDS = 80  # Target distilled length


def abstractive_distill(context: str, focus: str = "") -> str:
    """Use the model itself to compress context into a dense summary."""
    focus_clause = f" focusing on information relevant to: {focus}" if focus else ""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=TARGET_WORDS * 2,  # Words to tokens is ~1.3x
        messages=[{
            "role": "user",
            "content": (
                f"Distill this context into at most {TARGET_WORDS} words{focus_clause}. "
                "Preserve all specific facts, numbers, names, and decisions. Remove filler and redundancy.\n\n"
                f"CONTEXT:\n{context}"
            )
        }],
    )
    return response.content[0].text.strip()


def run_agent_with_abstractive_distillation(verbose_context: str, question: str) -> str:
    original_tokens = len(verbose_context) // 4

    # Distill with focus on the question
    distilled = abstractive_distill(verbose_context, focus=question)
    distilled_tokens = len(distilled) // 4

    savings_pct = (1 - distilled_tokens / original_tokens) * 100
    print(f"[distill] ~{original_tokens} -> ~{distilled_tokens} tokens (saved ~{savings_pct:.0f}%)")
    print(f"[distilled]\n{distilled}\n")

    # Now answer the question using distilled context
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Reference context:\n{distilled}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


verbose_incident_report = """
On March 15th 2024, at approximately 14:32 UTC, the production API began returning 503 errors.
The on-call engineer Sarah Chen received a PagerDuty alert at 14:33 UTC. Initial investigation showed
CPU utilization on the web tier had spiked to 98%. The database showed no unusual activity.
At 14:45 UTC, the team identified that a new deployment at 14:28 UTC had introduced a change to the
request handling middleware. Specifically, a new logging statement was synchronous and blocking the
event loop. The deployment was rolled back at 14:52 UTC. Normal operation resumed at 14:54 UTC.
Total incident duration: 22 minutes. Affected requests: approximately 18,000. Error rate during incident:
67%. Root cause: synchronous logging in async middleware introduced by pull request #4821.
The fix was to use asyncio-compatible logging. Post-incident review scheduled for March 22nd.
Action items: (1) Add async linting rules to CI, (2) Add event loop blocking detection to staging,
(3) Review all middleware changes for async compatibility before merging. The incident was classified
as P1. Customer impact: moderate. Three enterprise customers reported issues via support tickets.
"""

result = run_agent_with_abstractive_distillation(
    verbose_incident_report,
    "What was the root cause of the incident and how long did it last?"
)
print(f"Answer: {result}")

# Expected Token Savings: 50-75%; abstractive distillation creates denser summaries than extractive selection
# Environment: Python 3.11+; distillation adds ~100 tokens overhead but saves 500-2000 on large contexts
```

## Option 3: Hierarchical Distillation for Long Documents

```python
import anthropic

client = anthropic.Anthropic()

CHUNK_SIZE = 1500    # Characters per chunk
CHUNK_OVERLAP = 100  # Character overlap between chunks


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        start = end - overlap if end < len(text) else len(text)
    return chunks


def distill_chunk(chunk: str, chunk_idx: int, total_chunks: int) -> str:
    """Distill one chunk to its essential facts."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": (
                f"Extract the 3 most important facts from this text section ({chunk_idx+1}/{total_chunks}). "
                "Be extremely concise. Bullet points only.\n\n" + chunk
            )
        }],
    )
    return response.content[0].text.strip()


def hierarchical_distill(document: str) -> str:
    """
    Level 1: Distill each chunk independently.
    Level 2: Distill the combined chunk summaries into a final dense summary.
    """
    chunks = chunk_text(document)
    print(f"[distill] {len(chunks)} chunks from {len(document):,} chars")

    # Level 1: Chunk distillation
    chunk_summaries = [distill_chunk(chunk, i, len(chunks)) for i, chunk in enumerate(chunks)]
    combined = "\n\n".join(f"[Section {i+1}]\n{s}" for i, s in enumerate(chunk_summaries))

    # Level 2: Global distillation
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": "Merge these section summaries into one coherent, dense summary. Eliminate redundancy. Max 150 words.\n\n" + combined
        }],
    )
    final_summary = response.content[0].text.strip()
    original_tokens = len(document) // 4
    final_tokens = len(final_summary) // 4
    print(f"[distill] Final: ~{original_tokens} -> ~{final_tokens} tokens ({final_tokens/original_tokens:.0%})")
    return final_summary


def run_agent_hierarchical(long_document: str, question: str) -> str:
    distilled = hierarchical_distill(long_document)
    print(f"\n[distilled summary]\n{distilled}\n")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Document summary:\n{distilled}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


# Simulate a long technical document
long_doc = """
Python's garbage collection uses reference counting as its primary mechanism. Every object maintains a count of
references pointing to it. When the count drops to zero, the memory is immediately reclaimed. This is fast and
predictable. However, reference counting cannot handle cyclic references. To address this, Python uses a
supplementary cyclic garbage collector.

The cyclic garbage collector works by identifying groups of objects that are only reachable from each other
(reference cycles). It uses a generational approach with three generations. Objects that survive garbage
collection get promoted to older generations. Older generations are collected less frequently.

Generation 0 is collected most often, typically when it reaches 700 objects. Generation 1 is collected after
generation 0 has been collected 10 times. Generation 2 is the oldest and is collected rarely. The thresholds
are tunable via gc.set_threshold(). The gc module provides control over the garbage collector.

Memory fragmentation is another concern in Python. The small object allocator (obmalloc) manages objects
smaller than 512 bytes. It uses pools of memory divided into arenas. Each arena is 256KB. Pools within arenas
are divided into blocks of fixed size. This reduces fragmentation for small objects.

The __del__ finalizer method complicates garbage collection. Objects with __del__ in a reference cycle cannot
be collected automatically and are placed in gc.garbage. Python 3.4+ handles this better through PEP 442.
WeakRef objects allow references that don't prevent garbage collection. They're useful for caches and observer
patterns. The weakref module provides WeakValueDictionary and WeakSet for these use cases.

Memory profiling tools include tracemalloc (built-in), memory_profiler, and objgraph. tracemalloc can trace
memory allocations and show which lines of code allocated memory. sys.getsizeof() returns the size of an
object in bytes but doesn't include the size of referenced objects.
""" * 2  # Double it to make it longer

result = run_agent_hierarchical(long_doc, "How does Python's garbage collection handle circular references?")
print(f"Answer: {result}")

# Expected Token Savings: 70-85% on long documents; hierarchical approach handles arbitrarily long inputs
# Environment: Python 3.11+; tune CHUNK_SIZE (1000-3000) based on model's per-chunk performance; parallelize chunk distillation
```

## Option 4: Entity-Centric Distillation with Knowledge Graph

```python
import anthropic
import json
import re

client = anthropic.Anthropic()


def extract_entities_and_relations(text: str) -> dict:
    """Extract entities and their relationships from text."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                "Extract entities and key facts from this text. "
                "Respond with JSON: {\"entities\": [{\"name\": str, \"type\": str, \"facts\": [str]}], "
                "\"key_numbers\": [{\"value\": str, \"context\": str}]}\n\n" + text
            )
        }],
    )
    try:
        return json.loads(response.content[0].text.strip())
    except Exception:
        return {"entities": [], "key_numbers": []}


def entities_to_prompt(entities: dict, focus_entity: str | None = None) -> str:
    """Convert extracted entities into a compact prompt-ready format."""
    lines = []

    if focus_entity:
        # Filter to focus entity and its direct relations
        relevant = [e for e in entities.get("entities", [])
                    if focus_entity.lower() in e["name"].lower() or
                    any(focus_entity.lower() in f.lower() for f in e.get("facts", []))]
        entities_to_use = relevant or entities.get("entities", [])
    else:
        entities_to_use = entities.get("entities", [])

    for entity in entities_to_use[:6]:  # Limit to 6 most relevant entities
        facts = entity.get("facts", [])[:3]  # Top 3 facts per entity
        facts_str = "; ".join(facts) if facts else "mentioned"
        lines.append(f"{entity['name']} ({entity['type']}): {facts_str}")

    for kn in entities.get("key_numbers", [])[:4]:
        lines.append(f"Key: {kn['value']} — {kn['context']}")

    return "\n".join(lines)


def run_entity_distilled_agent(verbose_context: str, question: str) -> str:
    original_tokens = len(verbose_context) // 4

    # Extract key entities
    entities = extract_entities_and_relations(verbose_context)

    # Build focused distillation
    focus_terms = re.findall(r'\b[A-Z][a-z]+\b', question)
    focus = focus_terms[0] if focus_terms else None
    distilled = entities_to_prompt(entities, focus_entity=focus)
    distilled_tokens = len(distilled) // 4

    print(f"[entity distill] ~{original_tokens} -> ~{distilled_tokens} tokens")
    print(f"[entities found] {len(entities.get('entities', []))}")
    print(f"[distilled]\n{distilled}\n")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Relevant facts:\n{distilled}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


tech_context = """
Django was created by Adrian Holovaty and Simon Willison while working at the Lawrence Journal-World newspaper.
It was released publicly under a BSD license in July 2005. Django is written in Python and follows the
Model-View-Template (MVT) architectural pattern. The Django Software Foundation maintains the project.
Version 4.0 was released in December 2021 and added support for Python 3.8+.
FastAPI was created by Sebastián Ramírez (tiangolo) and first released in 2018. It is built on Starlette for
the web parts and Pydantic for the data parts. FastAPI automatically generates OpenAPI documentation.
FastAPI is known for high performance, on par with NodeJS and Go. It uses Python type hints extensively.
Flask was created by Armin Ronacher as an April Fools' joke in 2010 that became real. It is part of the
Pallets project. Flask follows the WSGI standard. It is considered a microframework because it keeps the
core simple but extensible. Flask 2.0 was released in May 2021 and added native async support.
"""

result = run_entity_distilled_agent(tech_context, "When was FastAPI created and who made it?")
print(f"Answer: {result}")

# Expected Token Savings: 65-80%; entity extraction preserves facts while discarding narrative structure
# Environment: Python 3.11+; entity extraction adds ~200 token overhead; break-even at ~800 token contexts
```

## Option 5: Query-Focused Distillation with Relevance Scoring

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

RELEVANCE_THRESHOLD = 6  # 0-10 scale


async def score_paragraph_relevance(paragraph: str, query: str) -> tuple[str, float]:
    """Score how relevant a paragraph is to the query."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": f"Rate relevance 0-10: Query: '{query}'\nParagraph: {paragraph[:200]}\nRespond with just the number."
        }],
    )
    try:
        score = float(response.content[0].text.strip().split()[0])
        return paragraph, score
    except Exception:
        return paragraph, 5.0


async def query_focused_distill(document: str, query: str, max_paragraphs: int = 4) -> str:
    """Keep only the paragraphs most relevant to the query."""
    paragraphs = [p.strip() for p in document.split("\n\n") if p.strip()]
    if len(paragraphs) <= max_paragraphs:
        return document

    # Score all paragraphs in parallel
    tasks = [asyncio.create_task(score_paragraph_relevance(p, query)) for p in paragraphs]
    scored: list[tuple[str, float]] = await asyncio.gather(*tasks)

    # Filter and sort by relevance
    relevant = [(p, s) for p, s in scored if s >= RELEVANCE_THRESHOLD]
    relevant.sort(key=lambda x: -x[1])
    top = relevant[:max_paragraphs]

    print(f"[relevance] {len(relevant)}/{len(paragraphs)} paragraphs above threshold {RELEVANCE_THRESHOLD}")
    for p, s in top:
        print(f"  score={s:.1f} | {p[:60]}...")

    if not top:
        # Fallback: take top N by score even if below threshold
        top = sorted(scored, key=lambda x: -x[1])[:max_paragraphs]

    return "\n\n".join(p for p, _ in top)


async def run_query_focused_agent(document: str, question: str) -> str:
    original_tokens = len(document) // 4
    distilled = await query_focused_distill(document, question, max_paragraphs=3)
    distilled_tokens = len(distilled) // 4

    print(f"\n[distill] ~{original_tokens} -> ~{distilled_tokens} tokens ({distilled_tokens/original_tokens:.0%})")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Relevant context:\n{distilled}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


multi_para_doc = """
Python's asyncio module was first added in Python 3.4 as a provisional package. It became non-provisional in Python 3.6.
The async/await syntax was introduced in Python 3.5 via PEP 492.

Asyncio provides infrastructure for writing single-threaded concurrent code using coroutines. The event loop is the
core of asyncio. It runs asynchronous tasks and callbacks, handles network IO, and runs subprocesses.

Django is a high-level Python web framework that encourages rapid development. It was designed to help developers
take applications from concept to completion as quickly as possible. Django includes an ORM, admin interface,
authentication, and many other batteries-included features.

The asyncio event loop manages and distributes execution of different tasks. When a coroutine awaits on an IO
operation, the event loop can run other coroutines while waiting. This allows efficient handling of many
concurrent connections without threads.

Flask is a micro web framework written in Python. It is classified as a microframework because it does not require
particular tools or libraries. It has no database abstraction layer or form validation.

asyncio.gather() runs multiple coroutines concurrently and returns their results. asyncio.wait() provides more
control over how results are collected. asyncio.TaskGroup (Python 3.11+) provides structured concurrency.
"""

result = asyncio.run(run_query_focused_agent(multi_para_doc, "How does asyncio's event loop work?"))
print(f"\nAnswer: {result}")

# Expected Token Savings: 40-70%; query-focused selection eliminates off-topic paragraphs entirely
# Environment: Python 3.11+; parallel scoring adds latency but is faster than sequential; cache scores for repeated queries
```

## Option 6: Progressive Distillation with Quality Verification

```python
import anthropic

client = anthropic.Anthropic()

DISTILLATION_LEVELS = [
    (0.50, 200),   # Level 1: compress to 50% of original, max 200 words
    (0.25, 100),   # Level 2: compress to 25%, max 100 words
    (0.10, 50),    # Level 3: compress to 10%, max 50 words
]


def distill_to_target(text: str, target_fraction: float, max_words: int) -> str:
    """Compress text to target fraction of original length."""
    target_words = min(max_words, max(20, int(len(text.split()) * target_fraction)))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=target_words * 2,
        messages=[{
            "role": "user",
            "content": (
                f"Compress this to at most {target_words} words. "
                "Preserve: specific facts, numbers, names, conclusions. Remove: examples, filler, repetition.\n\n"
                f"{text}"
            )
        }],
    )
    return response.content[0].text.strip()


def verify_distillation(original: str, distilled: str, key_question: str) -> bool:
    """Check that distilled version still answers the key question correctly."""
    orig_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Based on this text, answer briefly: {key_question}\n\n{original[:2000]}"}],
    )
    distil_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Based on this text, answer briefly: {key_question}\n\n{distilled}"}],
    )
    orig_ans = orig_response.content[0].text.lower()
    distil_ans = distil_response.content[0].text.lower()

    # Simple overlap check — in production, use embedding similarity
    orig_words = set(orig_ans.split())
    distil_words = set(distil_ans.split())
    overlap = len(orig_words & distil_words) / max(len(orig_words), 1)
    return overlap >= 0.4


def progressive_distill(context: str, key_question: str, target_level: int = 1) -> str:
    """
    Progressively compress, verifying quality at each level.
    Stops at the most compressed level that still answers the key question.
    """
    current = context
    best_valid = context
    original_tokens = len(context) // 4

    for level, (fraction, max_words) in enumerate(DISTILLATION_LEVELS[:target_level + 1], 1):
        distilled = distill_to_target(current, fraction, max_words)
        distilled_tokens = len(distilled) // 4
        savings = (1 - distilled_tokens / original_tokens) * 100

        valid = verify_distillation(context, distilled, key_question)
        status = "✓" if valid else "✗"
        print(f"[level {level}] {status} ~{distilled_tokens} tokens ({savings:.0f}% saved) | valid={valid}")

        if valid:
            best_valid = distilled
            current = distilled
        else:
            print(f"  Quality degraded at level {level} — stopping")
            break

    return best_valid


def run_progressive_agent(verbose_context: str, question: str, target_level: int = 2) -> str:
    distilled = progressive_distill(verbose_context, question, target_level=target_level)
    print(f"\n[final distilled]\n{distilled}\n")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Context:\n{distilled}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


verbose_text = """
The Python Enhancement Proposal (PEP) process is the mechanism by which changes to Python are proposed and evaluated.
A PEP is a design document providing information to the Python community, or describing a new feature for Python or
its processes or environment. PEPs provide a concise technical specification of the feature and a rationale for it.
Guido van Rossum, Python's creator, served as Python's Benevolent Dictator For Life (BDFL) until July 2018, when
he stepped back from the role. A five-person Steering Council now governs Python development decisions. PEP 8 is the
style guide for Python code, recommending 4 spaces for indentation, 79 character line limits, and naming conventions.
PEP 20 is the Zen of Python, which contains 19 aphorisms about Python design philosophy, including 'Simple is better
than complex' and 'Readability counts'. PEP 572 introduced the walrus operator (:=) in Python 3.8. PEP 634 introduced
structural pattern matching (match/case) in Python 3.10. PEP 3107 introduced function annotations in Python 3.0.
The Python Software Foundation (PSF) is the non-profit organization that manages Python's intellectual property and
funds Python development. The PSF was founded in 2001. Python is used extensively in data science, machine learning,
web development, automation, and scientific computing.
""" * 2

result = run_progressive_agent(verbose_text, "What is PEP 572 and when was it introduced?", target_level=2)
print(f"Answer: {result}")

# Expected Token Savings: 50-80% with quality verification; stops before quality degrades, unlike fixed-ratio compression
# Environment: Python 3.11+; verify_distillation adds 2 API calls overhead — break-even at contexts > 500 tokens saved
```

## Comparison

| Option | Distillation Method | Quality Check | Query-Focused | Async | Best For |
|--------|-------------------|--------------|---------------|-------|----------|
| 1. Extractive | Select top sentences | No | Partial (position) | No | Fast, no extra API call |
| 2. Abstractive (LLM) | LLM rewrite | No | Yes (focus param) | No | Best compression quality |
| 3. Hierarchical | Chunk → merge | No | No | No | Very long documents (10k+ chars) |
| 4. Entity-Centric | Extract entities + facts | No | Yes (entity focus) | No | Fact-dense technical docs |
| 5. Query-Focused | Relevance score per para | Via relevance | Yes | Yes | Multi-topic documents |
| 6. Progressive | Multi-level + verify | Yes | Yes | No | High-stakes production pipelines |
