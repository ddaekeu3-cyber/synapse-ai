---
layout: solution
title: "Agent Doesn't Implement Prompt Compression for Long Contexts"
category: prompt-engineering
description: "Agent sends full verbatim context on every call — long documents, entire conversation histories, raw tool outputs — instead of compressing them to fit more information in fewer tokens."
tags: [prompt-engineering, compression, context-window, tokens, summarization, efficiency]
---

# Agent Doesn't Implement Prompt Compression for Long Contexts

## Problem

An agent working with a 50-page document sends the full text on every call, consuming 40,000 tokens per request. After 5 turns this costs more tokens than a small fine-tuned model. Without compression, the agent hits context limits, costs balloon, and latency grows linearly with document size.

---

## Option 1: Extractive Sentence Compression

Score each sentence by relevance to the query using TF-IDF-style term overlap, keep only the top-scoring sentences, and reconstruct a compressed context.

```python
import anthropic
import re
from dataclasses import dataclass

@dataclass
class CompressedContext:
    original_length: int
    compressed_length: int
    compression_ratio: float
    text: str

def tokenize_text(text: str) -> list[str]:
    return re.findall(r'\b[a-z]{3,}\b', text.lower())

def score_sentence(sentence: str, query_tokens: set[str]) -> float:
    sent_tokens = set(tokenize_text(sentence))
    if not sent_tokens:
        return 0.0
    overlap = sent_tokens & query_tokens
    return len(overlap) / len(sent_tokens)

def extractive_compress(document: str, query: str, keep_ratio: float = 0.4) -> CompressedContext:
    query_tokens = set(tokenize_text(query))
    sentences = re.split(r'(?<=[.!?])\s+', document.strip())
    scored = [(score_sentence(s, query_tokens), i, s) for i, s in enumerate(sentences)]
    scored.sort(reverse=True)
    keep_n = max(1, int(len(sentences) * keep_ratio))
    kept = sorted(scored[:keep_n], key=lambda x: x[1])  # restore original order
    compressed = " ".join(s for _, _, s in kept)
    return CompressedContext(
        original_length=len(document),
        compressed_length=len(compressed),
        compression_ratio=len(compressed) / max(len(document), 1),
        text=compressed
    )

client = anthropic.Anthropic()

def answer_with_compression(document: str, query: str) -> str:
    compressed = extractive_compress(document, query, keep_ratio=0.35)
    print(f"Compression: {compressed.original_length} → {compressed.compressed_length} chars "
          f"({compressed.compression_ratio:.0%})")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Context (compressed):\n{compressed.text}\n\nQuestion: {query}"
        }]
    )
    return response.content[0].text

document = """
Machine learning is a subset of artificial intelligence that enables systems to learn from data.
The field was founded in 1956 at the Dartmouth Conference.
Neural networks are inspired by biological neurons in the human brain.
Deep learning uses multiple layers of neural networks to process complex patterns.
Supervised learning requires labeled training data to teach the model.
Unsupervised learning finds patterns in data without explicit labels.
Reinforcement learning trains agents through reward and punishment signals.
Natural language processing enables computers to understand human language.
Computer vision allows machines to interpret and analyze visual information.
Transfer learning applies knowledge from one domain to solve problems in another.
The transformer architecture revolutionized NLP starting with the 2017 paper "Attention Is All You Need".
Large language models are pre-trained on vast amounts of text data.
Fine-tuning adapts pre-trained models to specific downstream tasks.
Overfitting occurs when a model memorizes training data instead of learning general patterns.
Regularization techniques like dropout help prevent overfitting.
"""

result = answer_with_compression(document, "What is deep learning and how does it work?")
print(result)

# Expected Token Savings: 35% keep ratio reduces a 3000-token document to ~1050 tokens. On 10 calls, saves ~19,500 input tokens (65% reduction).
# Environment: ANTHROPIC_API_KEY required. Uses re module (stdlib only).
```

---

## Option 2: LLM-Based Abstractive Compression

Use a cheap model (Haiku) to summarize long contexts into a dense abstract before passing to the main model.

```python
import anthropic
from dataclasses import dataclass

@dataclass
class AbstractSummary:
    original_chars: int
    summary_chars: int
    ratio: float
    summary: str
    key_facts: list[str]

client = anthropic.Anthropic()

def abstractive_compress(document: str, query: str, target_words: int = 150) -> AbstractSummary:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Compress this document to ~{target_words} words, preserving information relevant to: "{query}"

Document:
{document}

Output format:
SUMMARY: [compressed summary]
KEY_FACTS: [bullet list of 3-5 critical facts]"""
        }]
    )
    text = response.content[0].text
    summary = ""
    key_facts = []
    for line in text.split("\n"):
        if line.startswith("SUMMARY:"):
            summary = line[8:].strip()
        elif line.startswith("KEY_FACTS:"):
            pass
        elif line.strip().startswith("-") or line.strip().startswith("•"):
            key_facts.append(line.strip().lstrip("-•").strip())

    return AbstractSummary(
        original_chars=len(document),
        summary_chars=len(summary),
        ratio=len(summary) / max(len(document), 1),
        summary=summary,
        key_facts=key_facts
    )

def two_stage_answer(document: str, query: str) -> str:
    compressed = abstractive_compress(document, query, target_words=120)
    print(f"Abstractive compression: {compressed.original_chars} → {compressed.summary_chars} chars "
          f"({compressed.ratio:.0%})")
    if compressed.key_facts:
        print(f"Key facts extracted: {len(compressed.key_facts)}")

    context = compressed.summary
    if compressed.key_facts:
        context += "\n\nKey facts:\n" + "\n".join(f"• {f}" for f in compressed.key_facts)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {query}"
        }]
    )
    return response.content[0].text

document = """
The Python programming language was created by Guido van Rossum and first released in 1991.
Python emphasizes code readability and uses significant whitespace indentation.
The language supports multiple programming paradigms including procedural, object-oriented, and functional programming.
Python 2 reached end-of-life on January 1, 2020, and Python 3 is now the standard.
The Python Package Index (PyPI) hosts over 400,000 packages as of 2024.
Popular frameworks include Django and Flask for web development, NumPy and Pandas for data science,
TensorFlow and PyTorch for machine learning, and FastAPI for building APIs.
Python's Global Interpreter Lock (GIL) prevents true multi-threaded parallelism for CPU-bound tasks.
The language uses dynamic typing and automatic memory management through garbage collection.
List comprehensions, generators, and decorators are powerful Pythonic idioms.
The Zen of Python (PEP 20) outlines the guiding principles of the language design.
CPython is the reference implementation written in C, but alternatives exist: PyPy (JIT), Jython (JVM), IronPython (.NET).
Virtual environments isolate project dependencies using venv or conda.
Type hints were introduced in Python 3.5 via PEP 484 to enable static type checking with tools like mypy.
"""

result = two_stage_answer(document, "What are Python's limitations for parallel computing?")
print(f"\nAnswer: {result}")

# Expected Token Savings: Haiku summarization costs ~300 tokens. Reduces 2000-token document to ~400 tokens for Sonnet call. Net savings: ~1300 tokens per query (65%). Break-even at 1 use.
# Environment: ANTHROPIC_API_KEY required. Uses claude-haiku-4-5-20251001 for compression, claude-sonnet-4-6 for final answer.
```

---

## Option 3: Sliding Window Compression with Rolling Summary

For multi-turn conversations, compress old turns into a rolling summary rather than passing the full history.

```python
import anthropic
from dataclasses import dataclass

@dataclass
class ConversationState:
    rolling_summary: str
    recent_turns: list[dict]
    total_turns: int
    compressed_turns: int
    summary_tokens_estimate: int

client = anthropic.Anthropic()

MAX_RECENT_TURNS = 4
SUMMARY_TARGET_WORDS = 100

def compress_turns_to_summary(turns: list[dict], existing_summary: str) -> str:
    turns_text = "\n".join(
        f"{t['role'].upper()}: {t['content']}"
        for t in turns
    )
    context = f"Existing summary:\n{existing_summary}\n\n" if existing_summary else ""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""{context}Compress these conversation turns into a ~{SUMMARY_TARGET_WORDS}-word summary preserving key facts and decisions:

{turns_text}"""
        }]
    )
    return response.content[0].text.strip()

def add_turn(state: ConversationState, role: str, content: str) -> ConversationState:
    state.recent_turns.append({"role": role, "content": content})
    state.total_turns += 1

    if len(state.recent_turns) > MAX_RECENT_TURNS * 2:  # *2 for user+assistant pairs
        to_compress = state.recent_turns[:-MAX_RECENT_TURNS]
        state.rolling_summary = compress_turns_to_summary(to_compress, state.rolling_summary)
        state.recent_turns = state.recent_turns[-MAX_RECENT_TURNS:]
        state.compressed_turns += len(to_compress)
        state.summary_tokens_estimate = len(state.rolling_summary.split()) * 4 // 3
        print(f"[compress] {len(to_compress)} turns compressed. Summary: {state.summary_tokens_estimate} tokens")

    return state

def build_messages(state: ConversationState) -> list[dict]:
    messages = []
    if state.rolling_summary:
        messages.append({
            "role": "user",
            "content": f"[Conversation summary so far]\n{state.rolling_summary}"
        })
        messages.append({
            "role": "assistant",
            "content": "I understand the context from our previous conversation."
        })
    messages.extend(state.recent_turns)
    return messages

def chat(state: ConversationState, user_message: str) -> tuple[str, ConversationState]:
    state = add_turn(state, "user", user_message)
    messages = build_messages(state)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=messages
    )
    reply = response.content[0].text
    state = add_turn(state, "assistant", reply)
    return reply, state

state = ConversationState(
    rolling_summary="", recent_turns=[], total_turns=0,
    compressed_turns=0, summary_tokens_estimate=0
)

conversations = [
    "My name is Alex and I'm building a Python web scraper.",
    "I'm using BeautifulSoup and requests libraries.",
    "The site uses JavaScript rendering so I'm considering Playwright.",
    "I need to scrape 10,000 pages per day.",
    "What's the best way to handle rate limiting?",
    "Should I use async or threading for this scale?",
]

for msg in conversations:
    reply, state = chat(state, msg)
    print(f"User: {msg}")
    print(f"Agent: {reply[:100]}\n")

print(f"Total turns: {state.total_turns}, Compressed: {state.compressed_turns}")
print(f"Recent in context: {len(state.recent_turns)}, Summary tokens: {state.summary_tokens_estimate}")

# Expected Token Savings: After 10 turns, rolling summary holds 6 compressed turns in ~150 tokens vs ~1500 tokens of full history. 90% history compression on long conversations.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 4: Hierarchical Document Chunking and Selective Loading

Split large documents into chunks, score each chunk for relevance, and load only the top-k most relevant chunks into context.

```python
import anthropic
import re
from dataclasses import dataclass

@dataclass
class Chunk:
    chunk_id: int
    text: str
    tokens_estimate: int
    relevance_score: float = 0.0

def estimate_tokens(text: str) -> int:
    return len(text.split()) * 4 // 3

def split_into_chunks(document: str, max_chunk_words: int = 100) -> list[Chunk]:
    paragraphs = [p.strip() for p in document.split("\n\n") if p.strip()]
    chunks = []
    current_chunk = []
    current_words = 0

    for para in paragraphs:
        words = len(para.split())
        if current_words + words > max_chunk_words and current_chunk:
            text = "\n".join(current_chunk)
            chunks.append(Chunk(len(chunks), text, estimate_tokens(text)))
            current_chunk = [para]
            current_words = words
        else:
            current_chunk.append(para)
            current_words += words

    if current_chunk:
        text = "\n".join(current_chunk)
        chunks.append(Chunk(len(chunks), text, estimate_tokens(text)))
    return chunks

def score_chunks_by_overlap(chunks: list[Chunk], query: str) -> list[Chunk]:
    query_tokens = set(re.findall(r'\b[a-z]{3,}\b', query.lower()))
    for chunk in chunks:
        chunk_tokens = set(re.findall(r'\b[a-z]{3,}\b', chunk.text.lower()))
        chunk.relevance_score = len(chunk_tokens & query_tokens) / max(len(query_tokens), 1)
    return sorted(chunks, key=lambda c: c.relevance_score, reverse=True)

client = anthropic.Anthropic()

def answer_with_chunked_context(document: str, query: str, token_budget: int = 800) -> str:
    chunks = split_into_chunks(document, max_chunk_words=80)
    scored = score_chunks_by_overlap(chunks, query)

    selected = []
    total_tokens = 0
    for chunk in scored:
        if total_tokens + chunk.tokens_estimate <= token_budget:
            selected.append(chunk)
            total_tokens += chunk.tokens_estimate
        if total_tokens >= token_budget:
            break

    # Restore original order for coherence
    selected.sort(key=lambda c: c.chunk_id)
    context = "\n\n".join(c.text for c in selected)

    print(f"Chunks: {len(chunks)} total, {len(selected)} selected")
    print(f"Token budget: {token_budget}, used: {total_tokens}")
    print(f"Coverage: {total_tokens / estimate_tokens(document):.0%} of document")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Relevant document excerpts:\n{context}\n\nQuestion: {query}"
        }]
    )
    return response.content[0].text

document = "\n\n".join([
    "Kubernetes is an open-source container orchestration platform developed by Google.",
    "Pods are the smallest deployable units in Kubernetes, containing one or more containers.",
    "Services provide stable network endpoints to expose pods to other applications.",
    "Deployments manage the desired state of pod replicas and handle rolling updates.",
    "ConfigMaps store non-sensitive configuration data as key-value pairs.",
    "Secrets store sensitive data like passwords and API keys in base64-encoded format.",
    "Namespaces provide virtual clusters within a single physical cluster.",
    "Ingress controllers manage external HTTP/HTTPS access to cluster services.",
    "Persistent Volumes decouple storage from pod lifecycle for stateful workloads.",
    "Horizontal Pod Autoscalers automatically scale replica count based on CPU or custom metrics.",
    "Node affinity rules control which nodes pods can be scheduled on.",
    "Resource limits and requests define CPU and memory bounds for containers.",
    "The control plane includes the API server, scheduler, and controller manager.",
    "Worker nodes run the kubelet agent and container runtime like containerd or Docker.",
    "Helm is the package manager for Kubernetes, using charts to define applications.",
])

result = answer_with_chunked_context(document, "How does Kubernetes handle storage for stateful apps?", token_budget=400)
print(f"\nAnswer: {result}")

# Expected Token Savings: 800-token budget on a 2000-token document saves 1200 tokens (60%) per call. Relevance scoring ensures quality isn't lost despite compression.
# Environment: ANTHROPIC_API_KEY required. Uses re module (stdlib).
```

---

## Option 5: Token-Budget-Aware Prompt Assembly

Assemble prompts from prioritized components, fitting as many high-priority elements as possible within a hard token budget.

```python
import anthropic
from dataclasses import dataclass
from typing import Optional

@dataclass
class PromptComponent:
    name: str
    content: str
    priority: int  # lower = higher priority
    required: bool = False

    @property
    def token_estimate(self) -> int:
        return len(self.content.split()) * 4 // 3

def assemble_prompt(
    components: list[PromptComponent],
    token_budget: int,
    separator: str = "\n\n"
) -> tuple[str, list[str], int]:
    """
    Returns (assembled_prompt, included_names, total_tokens_used)
    """
    required = [c for c in components if c.required]
    optional = sorted([c for c in components if not c.required], key=lambda c: c.priority)

    selected = []
    tokens_used = 0

    # Always include required components
    for comp in required:
        selected.append(comp)
        tokens_used += comp.token_estimate

    # Fill remaining budget with optional by priority
    for comp in optional:
        if tokens_used + comp.token_estimate <= token_budget:
            selected.append(comp)
            tokens_used += comp.token_estimate

    selected.sort(key=lambda c: c.priority)
    assembled = separator.join(c.content for c in selected)
    return assembled, [c.name for c in selected], tokens_used

client = anthropic.Anthropic()

def answer_with_budget_assembly(query: str, components: list[PromptComponent], budget: int = 600) -> str:
    prompt_body, included, tokens = assemble_prompt(components, budget)
    print(f"Included components: {included} ({tokens} tokens)")
    print(f"Excluded: {[c.name for c in components if c.name not in included]}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{prompt_body}\n\nQuestion: {query}"
        }]
    )
    return response.content[0].text

components = [
    PromptComponent("system_rules", "You are a helpful technical assistant. Be concise.", priority=0, required=True),
    PromptComponent("user_query_context", "The user is a senior Python developer debugging a FastAPI app.", priority=1),
    PromptComponent("recent_error", "Error: TypeError: 'NoneType' object is not subscriptable at line 42.", priority=2, required=True),
    PromptComponent("stack_trace", "Traceback:\n  File 'main.py', line 42, in get_user\n    return db.query(User)[0]\nTypeError: 'NoneType' object is not subscriptable", priority=3),
    PromptComponent("relevant_code", "def get_user(db: Session):\n    return db.query(User).first()", priority=4),
    PromptComponent("full_file_context", "# 300 lines of application code...\n" + "# line\n" * 50, priority=5),
    PromptComponent("database_schema", "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(255), email VARCHAR(255) UNIQUE);\n" * 10, priority=6),
]

result = answer_with_budget_assembly("Why am I getting this TypeError?", components, budget=500)
print(f"\nAnswer: {result}")

# Expected Token Savings: Budget assembly drops low-priority components automatically. A 2000-token prompt trimmed to 500 saves 75% per call. Required components always preserved for correctness.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 6: Cached Compressed Context with SQLite

Compress documents once, cache the compressed version in SQLite, and reuse across calls with the same document.

```python
import anthropic
import sqlite3
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class CachedCompression:
    doc_hash: str
    query_hash: str
    original_length: int
    compressed_text: str
    compression_ratio: float
    model_used: str
    created_at: float
    hit_count: int = 0

client = anthropic.Anthropic()

def init_compression_cache(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compression_cache (
            cache_key TEXT PRIMARY KEY,
            doc_hash TEXT,
            query_hash TEXT,
            original_length INTEGER,
            compressed_text TEXT,
            compression_ratio REAL,
            model_used TEXT,
            created_at REAL,
            hit_count INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn

def make_cache_key(document: str, query: str) -> tuple[str, str, str]:
    doc_hash = hashlib.sha256(document.encode()).hexdigest()[:16]
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:8]
    return f"{doc_hash}:{query_hash}", doc_hash, query_hash

def get_cached(conn: sqlite3.Connection, cache_key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT compressed_text FROM compression_cache WHERE cache_key=?",
        (cache_key,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE compression_cache SET hit_count = hit_count + 1 WHERE cache_key=?",
            (cache_key,)
        )
        conn.commit()
        return row[0]
    return None

def compress_and_cache(
    conn: sqlite3.Connection,
    document: str,
    query: str,
    target_words: int = 200
) -> str:
    cache_key, doc_hash, query_hash = make_cache_key(document, query)
    cached = get_cached(conn, cache_key)
    if cached:
        print(f"[cache hit] {cache_key[:16]}")
        return cached

    print(f"[compress] Calling Haiku to compress {len(document)} chars")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Compress this document to ~{target_words} words, keeping information relevant to: "{query}"

{document}

Return only the compressed text, no preamble."""
        }]
    )
    compressed = response.content[0].text.strip()
    ratio = len(compressed) / max(len(document), 1)

    conn.execute(
        "INSERT OR REPLACE INTO compression_cache VALUES (?,?,?,?,?,?,?,?,?)",
        (cache_key, doc_hash, query_hash, len(document), compressed, ratio,
         "claude-haiku-4-5-20251001", time.time(), 0)
    )
    conn.commit()
    print(f"[cached] ratio={ratio:.0%} ({len(document)} → {len(compressed)} chars)")
    return compressed

def answer_with_cached_compression(
    document: str,
    query: str,
    conn: sqlite3.Connection
) -> str:
    compressed = compress_and_cache(conn, document, query, target_words=150)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Context:\n{compressed}\n\nQuestion: {query}"
        }]
    )
    return response.content[0].text

conn = init_compression_cache()
document = """
Rust is a systems programming language focused on safety, speed, and concurrency.
It achieves memory safety without a garbage collector through its ownership system.
Every value in Rust has a single owner; when the owner goes out of scope, the value is dropped.
Borrowing allows references to values without taking ownership, with strict lifetime rules enforced at compile time.
The borrow checker prevents data races and use-after-free bugs at compile time.
Rust's type system includes algebraic data types through enums and pattern matching.
The Option type replaces null pointers; Result handles errors explicitly without exceptions.
Traits define shared behavior similar to interfaces in other languages.
Cargo is Rust's build system and package manager, handling dependencies via Cargo.toml.
Async/await support enables high-performance asynchronous programming with tokio or async-std runtimes.
"""

query = "How does Rust prevent memory safety bugs?"
result1 = answer_with_cached_compression(document, query, conn)
print(f"Answer 1: {result1[:100]}\n")

# Second call uses cache
result2 = answer_with_cached_compression(document, query, conn)
print(f"Answer 2 (cached): {result2[:100]}")

stats = conn.execute("SELECT SUM(hit_count), COUNT(*) FROM compression_cache").fetchone()
print(f"\nCache: {stats[1]} entries, {stats[0]} hits")

# Expected Token Savings: First call: ~300 tokens for compression. Subsequent calls: 0 compression tokens. After 5 reuses of same doc+query, total compression cost amortized to 60 tokens/call.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3, hashlib (stdlib).
```

---

## Comparison

| Option | Compression Method | Quality | Token Savings | Persistence | Best For |
|--------|-------------------|---------|---------------|-------------|----------|
| 1: Extractive Sentence | TF-IDF overlap | Medium | 50–65% | None | Fast, zero-LLM compression |
| 2: Abstractive (Haiku) | LLM summarization | High | 60–75% | None | Best quality, small extra cost |
| 3: Rolling Summary | Sliding window + LLM | High | 85–90% (history) | None | Long multi-turn conversations |
| 4: Chunk Selection | Relevance scoring | Medium-High | 40–70% | None | Large document QA |
| 5: Budget Assembly | Priority packing | Configurable | Up to 80% | None | Multi-component prompts |
| 6: Cached Compression | LLM + SQLite cache | High | 0 after first hit | SQLite | Repeated queries on same docs |
