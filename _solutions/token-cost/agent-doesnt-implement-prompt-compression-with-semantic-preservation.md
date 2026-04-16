---
layout: solution
title: "Agent Doesn't Implement Prompt Compression with Semantic Preservation"
category: token-cost
description: "Reduce token count in prompts by removing redundancy and compressing verbose context while preserving the meaning and intent needed for accurate responses."
tags: [token-cost, compression, prompt-engineering, context, efficiency, cost]
---

Agents often pass bloated prompts containing verbose instructions, repeated context, unnecessary preamble, or raw tool output that could be summarized. Prompt compression removes redundancy and condenses verbose content before sending to the model — cutting costs without sacrificing response quality, since the model receives the same semantically-equivalent information in fewer tokens.

## Option 1: Rule-Based Whitespace and Redundancy Removal

Apply a set of deterministic transformations: strip excess whitespace, collapse repeated phrases, remove filler words, and truncate over-padded sections. Fast, deterministic, and zero extra API calls.

```python
import anthropic
import re

def remove_redundant_whitespace(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)        # max 2 consecutive newlines
    text = re.sub(r"[ \t]{2,}", " ", text)         # collapse inline spaces
    text = re.sub(r"^\s+|\s+$", "", text, flags=re.MULTILINE)  # trim line edges
    return text.strip()

def remove_filler_phrases(text: str) -> str:
    fillers = [
        r"(?i)as (an? )?AI(?: language model)?,?\s*",
        r"(?i)certainly[!,]?\s*",
        r"(?i)of course[!,]?\s*",
        r"(?i)I('d| would) be happy to\s*",
        r"(?i)I('ll| will) now\s*",
        r"(?i)Please note that\s*",
        r"(?i)It(?: is|'s) (important|worth) (to note|noting) that\s*",
        r"(?i)Without further ado,?\s*",
    ]
    for pattern in fillers:
        text = re.sub(pattern, "", text)
    return text

def deduplicate_sentences(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    seen: set[str] = set()
    unique = []
    for s in sentences:
        normalized = re.sub(r"\s+", " ", s.lower().strip())
        if normalized not in seen:
            seen.add(normalized)
            unique.append(s)
    return " ".join(unique)

def compress_system_prompt(prompt: str) -> tuple[str, int, int]:
    original_len = len(prompt.split())
    prompt = remove_redundant_whitespace(prompt)
    prompt = remove_filler_phrases(prompt)
    prompt = deduplicate_sentences(prompt)
    compressed_len = len(prompt.split())
    return prompt, original_len, compressed_len

def call_with_compressed_prompt(
    system: str,
    user_message: str,
) -> str:
    compressed, orig_words, comp_words = compress_system_prompt(system)
    reduction = (1 - comp_words / orig_words) * 100 if orig_words else 0
    print(f"[Compression] {orig_words} → {comp_words} words ({reduction:.1f}% reduction)")

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=compressed,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# Demo
if __name__ == "__main__":
    verbose_system = """
    As an AI language model, I will be happy to assist you. Certainly, I am here to help.
    You are a helpful assistant. You are a helpful assistant who answers questions.
    Please note that you should always be accurate. It is important to note that accuracy matters.
    Please note that you should always be accurate and truthful in your responses.

    When answering questions, you should answer questions clearly and concisely.
    When answering questions, provide clear and concise answers.
    """
    result = call_with_compressed_prompt(verbose_system, "What is the capital of France?")
    print(result)

# Expected Token Savings: 15-40% on verbose system prompts with redundant instructions
# Environment: pip install anthropic
```

## Option 2: LLM-Based Semantic Compression

Use a cheap, fast model (Haiku) to rewrite a verbose prompt into its minimal semantically-equivalent form before sending it to the primary model. Pay a small upfront token cost in exchange for potentially large savings on repeated use with the same compressed prompt (especially with prompt caching).

```python
import anthropic
import hashlib

_compression_cache: dict[str, str] = {}

COMPRESSION_SYSTEM = """Compress the following text to its minimal form that preserves all meaning and instructions. Rules:
- Remove filler words and redundant phrases
- Merge duplicate instructions into one
- Use compact phrasing without losing precision
- Preserve all factual content and constraints
- Return ONLY the compressed text, no explanation"""

def compress_with_llm(text: str, client: anthropic.Anthropic) -> tuple[str, int, int]:
    cache_key = hashlib.md5(text.encode()).hexdigest()
    if cache_key in _compression_cache:
        compressed = _compression_cache[cache_key]
        return compressed, len(text.split()), len(compressed.split())

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(text.split()) + 50,  # compressed should be shorter
        system=COMPRESSION_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    compressed = response.content[0].text.strip()
    _compression_cache[cache_key] = compressed

    orig_tokens = response.usage.input_tokens
    comp_words = len(compressed.split())
    orig_words = len(text.split())
    print(f"[LLM-Compress] {orig_words}w → {comp_words}w ({(1-comp_words/orig_words)*100:.1f}% reduction). Compression cost: {orig_tokens} input tokens")
    return compressed, orig_words, comp_words

def call_with_llm_compression(
    system: str,
    messages: list[dict],
    compress_threshold_words: int = 100,
) -> str:
    client = anthropic.Anthropic()

    if len(system.split()) >= compress_threshold_words:
        system, _, _ = compress_with_llm(system, client)

    # Also compress any large user messages
    compressed_messages = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content.split()) >= compress_threshold_words:
            compressed_content, _, _ = compress_with_llm(content, client)
            compressed_messages.append({**msg, "content": compressed_content})
        else:
            compressed_messages.append(msg)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=compressed_messages,
    )
    return response.content[0].text

if __name__ == "__main__":
    verbose_system = """
    You are a senior software engineer with extensive experience in Python programming,
    software architecture, and best practices. You have been working in the field for
    over 15 years and have deep knowledge of algorithms, data structures, design patterns,
    and software engineering principles. When answering questions, you should provide
    technically accurate, detailed, and well-structured responses. You should consider
    edge cases, performance implications, and maintainability when giving advice.
    Always prefer clear, readable code over clever code. Always prefer simple solutions
    over complex ones unless complexity is justified. Explain your reasoning.
    """
    result = call_with_llm_compression(
        verbose_system,
        [{"role": "user", "content": "How should I handle exceptions in Python async code?"}],
    )
    print(result)

# Expected Token Savings: 30-60% on first use; 100% compression cost amortized on repeated calls
# Environment: pip install anthropic
```

## Option 3: Sliding Window Context Compression

For multi-turn conversations, older turns accumulate token cost every round. Compress old turns into a rolling summary while keeping recent turns verbatim. The model always sees a concise history plus full recent context.

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class CompressingConversation:
    recent_window: int = 6          # keep this many turns verbatim
    compress_batch: int = 4         # compress this many turns at a time
    _messages: list[dict] = field(default_factory=list)
    _summaries: list[str] = field(default_factory=list)
    _client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)

    def _compress_turns(self, turns: list[dict]) -> str:
        formatted = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in turns
        )
        response = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"Summarize this conversation excerpt in 2-3 sentences, preserving key facts and decisions:\n\n{formatted}",
            }],
        )
        return response.content[0].text.strip()

    def add_turn(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})
        # When old turns exceed the recent window, compress them
        compressible = len(self._messages) - self.recent_window
        if compressible >= self.compress_batch:
            batch = self._messages[:self.compress_batch]
            summary = self._compress_turns(batch)
            self._summaries.append(summary)
            self._messages = self._messages[self.compress_batch:]
            print(f"[SlidingCompress] Compressed {self.compress_batch} turns → 1 summary")

    def build_messages(self) -> list[dict]:
        """Return messages with compressed history injected as context."""
        if not self._summaries:
            return list(self._messages)

        summary_block = "CONVERSATION HISTORY SUMMARY:\n" + "\n".join(
            f"- {s}" for s in self._summaries
        )
        context_msg = {"role": "user", "content": summary_block}
        ack_msg = {"role": "assistant", "content": "Understood. Continuing with that context."}
        return [context_msg, ack_msg] + list(self._messages)

    def token_estimate(self) -> int:
        total_chars = sum(len(m["content"]) for m in self.build_messages())
        return total_chars // 4  # rough approximation

    def chat(self, user_message: str, system: str = "") -> str:
        self.add_turn("user", user_message)
        messages = self.build_messages()
        kwargs = {"model": "claude-sonnet-4-6", "max_tokens": 512, "messages": messages}
        if system:
            kwargs["system"] = system
        response = self._client.messages.create(**kwargs)
        reply = response.content[0].text
        self.add_turn("assistant", reply)
        print(f"[SlidingCompress] Context ~{self.token_estimate()} tokens, {len(self._summaries)} summaries")
        return reply

if __name__ == "__main__":
    conv = CompressingConversation(recent_window=4, compress_batch=4)
    system = "You are a helpful Python tutor."
    topics = [
        "What are Python decorators?",
        "Show me a simple decorator example",
        "How do I use functools.wraps?",
        "What about class-based decorators?",
        "Can decorators take arguments?",
        "How do I stack multiple decorators?",
        "What are some real-world uses of decorators?",
        "How does the @property decorator work?",
    ]
    for topic in topics:
        reply = conv.chat(topic, system)
        print(f"Q: {topic}\nA: {reply[:80]}...\n")

# Expected Token Savings: 40-70% reduction in context tokens for long conversations
# Environment: pip install anthropic
```

## Option 4: Selective Field Compression for Structured Data

When passing JSON tool results or structured data to the model, many fields are irrelevant to the current task. Extract only task-relevant fields and summarize arrays/blobs. Keeps the model focused and cuts tokens dramatically for API responses or database records.

```python
import anthropic
import json
from typing import Any

def compress_json_for_context(
    data: dict | list,
    keep_fields: set[str],
    max_array_items: int = 5,
    max_string_length: int = 200,
) -> Any:
    """Recursively prune and compress JSON for LLM context."""
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if keep_fields and key not in keep_fields:
                continue
            result[key] = compress_json_for_context(value, keep_fields, max_array_items, max_string_length)
        return result
    elif isinstance(data, list):
        if len(data) > max_array_items:
            compressed = [compress_json_for_context(item, keep_fields, max_array_items, max_string_length)
                          for item in data[:max_array_items]]
            return compressed + [f"... and {len(data) - max_array_items} more items"]
        return [compress_json_for_context(item, keep_fields, max_array_items, max_string_length) for item in data]
    elif isinstance(data, str) and len(data) > max_string_length:
        return data[:max_string_length] + f"... [{len(data) - max_string_length} chars truncated]"
    return data

def summarize_array_field(client: anthropic.Anthropic, items: list, context: str) -> str:
    """Use Haiku to summarize a large array into a sentence."""
    sample = json.dumps(items[:10], indent=2)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"Summarize these {context} items in one sentence:\n{sample}\n(Total: {len(items)} items)"}],
    )
    return response.content[0].text.strip()

def call_with_structured_compression(
    tool_result: dict,
    user_question: str,
    relevant_fields: set[str],
) -> str:
    client = anthropic.Anthropic()

    # Compress the tool result
    compressed = compress_json_for_context(tool_result, relevant_fields)
    compressed_json = json.dumps(compressed, indent=2)

    orig_tokens = len(json.dumps(tool_result)) // 4
    comp_tokens = len(compressed_json) // 4
    print(f"[StructCompress] ~{orig_tokens} → ~{comp_tokens} tokens ({(1-comp_tokens/orig_tokens)*100:.1f}% reduction)")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Tool result:\n```json\n{compressed_json}\n```\n\n{user_question}",
        }],
    )
    return response.content[0].text

if __name__ == "__main__":
    # Simulated large API response
    api_response = {
        "user": {
            "id": "u_123",
            "name": "Alice",
            "email": "alice@example.com",
            "created_at": "2024-01-15",
            "last_login": "2026-04-16",
            "preferences": {"theme": "dark", "language": "en", "notifications": True},
            "internal_flags": {"ab_group": "B", "feature_flags": ["flag_1", "flag_2"]},
            "audit_log": [{"action": f"login_{i}", "ts": f"2026-04-{i:02d}"} for i in range(1, 50)],
        },
        "subscription": {
            "plan": "pro",
            "status": "active",
            "renewal_date": "2026-05-16",
            "payment_method": "card_****4242",
        },
    }

    result = call_with_structured_compression(
        tool_result=api_response,
        user_question="When does this user's subscription renew and what plan are they on?",
        relevant_fields={"name", "plan", "status", "renewal_date", "subscription"},
    )
    print(result)

# Expected Token Savings: 50-90% when tool results contain large irrelevant fields
# Environment: pip install anthropic
```

## Option 5: Async Batch Compression Pipeline

For agents that build context incrementally (e.g. from multiple tool calls), compress each tool result asynchronously as it arrives. By the time all tools complete, compressed results are ready — no extra latency penalty.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass, field

@dataclass
class CompressionResult:
    original_content: str
    compressed_content: str
    original_tokens: int
    compressed_tokens: int
    source_label: str

async def compress_one(
    client: anthropic.AsyncAnthropic,
    content: str,
    label: str,
    target_sentences: int = 3,
) -> CompressionResult:
    if len(content.split()) < 50:
        return CompressionResult(content, content, len(content)//4, len(content)//4, label)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"Compress this into {target_sentences} dense sentences, preserving all key facts:\n\n{content}",
        }],
    )
    compressed = response.content[0].text.strip()
    return CompressionResult(
        original_content=content,
        compressed_content=compressed,
        original_tokens=response.usage.input_tokens,
        compressed_tokens=response.usage.output_tokens,
        source_label=label,
    )

async def parallel_compress_and_call(
    tool_outputs: dict[str, str],
    user_question: str,
    system: str = "",
) -> str:
    client = anthropic.AsyncAnthropic()

    # Compress all tool outputs concurrently
    compress_tasks = [
        compress_one(client, content, label)
        for label, content in tool_outputs.items()
    ]
    results: list[CompressionResult] = await asyncio.gather(*compress_tasks)

    total_orig = sum(r.original_tokens for r in results)
    total_comp = sum(r.compressed_tokens for r in results)
    print(f"[AsyncCompress] {total_orig} → {total_comp} tokens ({(1-total_comp/total_orig)*100:.1f}% reduction)")

    context_block = "\n\n".join(
        f"### {r.source_label}\n{r.compressed_content}"
        for r in results
    )
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": f"{context_block}\n\n{user_question}"}],
    }
    if system:
        kwargs["system"] = system
    response = await client.messages.create(**kwargs)
    return response.content[0].text

async def main():
    tool_outputs = {
        "web_search": """
        Python was created by Guido van Rossum and was first released in 1991. The language
        was designed with an emphasis on code readability and simplicity. Python supports
        multiple programming paradigms including procedural, object-oriented, and functional
        programming. It has a comprehensive standard library and a large ecosystem of
        third-party packages available through PyPI. Python is widely used in data science,
        machine learning, web development, automation, and scientific computing. The language
        uses indentation to define code blocks rather than curly braces.
        """,
        "database_query": """
        Retrieved 47 records from the python_projects table. Fields include: id, name,
        description, stars, forks, language, created_at, updated_at, owner_login.
        Top entries: django (73k stars, web framework), flask (66k stars, micro framework),
        requests (50k stars, HTTP library), numpy (25k stars, numerical computing),
        pandas (42k stars, data analysis), scikit-learn (58k stars, machine learning).
        Query executed in 23ms. Cache hit: false.
        """,
        "api_call": """
        GitHub API response for python language stats. Total repositories: 2.4 million.
        Active repositories (pushed in last year): 890,000. Contributors count: 5.2 million.
        Top topics: machine-learning (340k repos), data-science (280k repos), web-scraping
        (190k repos), automation (220k repos), django (150k repos). Pull requests merged
        last month: 1.2 million. Issues opened last month: 890,000.
        """,
    }

    result = await parallel_compress_and_call(
        tool_outputs,
        "Summarize Python's ecosystem health and primary use cases in 3 bullet points.",
    )
    print(result)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 40-65% on tool result context; zero added latency via parallelism
# Environment: pip install anthropic
```

## Option 6: Importance-Weighted Context Pruning

Score each piece of context by relevance to the current query using embedding similarity or keyword overlap. Discard low-relevance chunks and keep the most relevant ones up to a token budget. The model sees a richer, more focused context in fewer tokens.

```python
import anthropic
import math
import re
from dataclasses import dataclass

@dataclass
class ContextChunk:
    text: str
    source: str
    relevance_score: float = 0.0

def word_overlap_score(query: str, chunk: str) -> float:
    """Simple TF-style relevance: fraction of query words found in chunk."""
    query_words = set(re.findall(r"\w+", query.lower()))
    chunk_words = set(re.findall(r"\w+", chunk.lower()))
    if not query_words:
        return 0.0
    overlap = query_words & chunk_words
    # Boost for rarer query words (IDF approximation)
    score = sum(1 / math.log(len(w) + 2) for w in overlap)
    return score / len(query_words)

def select_chunks_by_budget(
    chunks: list[ContextChunk],
    token_budget: int,
    query: str,
) -> list[ContextChunk]:
    # Score all chunks
    for chunk in chunks:
        chunk.relevance_score = word_overlap_score(query, chunk.text)

    # Sort by relevance descending
    ranked = sorted(chunks, key=lambda c: c.relevance_score, reverse=True)

    selected = []
    used_tokens = 0
    for chunk in ranked:
        chunk_tokens = len(chunk.text.split()) * 4 // 3  # rough token estimate
        if used_tokens + chunk_tokens <= token_budget:
            selected.append(chunk)
            used_tokens += chunk_tokens
        if used_tokens >= token_budget:
            break

    # Restore original order for coherence
    original_order = {c.source: i for i, c in enumerate(chunks)}
    selected.sort(key=lambda c: original_order[c.source])

    dropped = len(chunks) - len(selected)
    print(f"[PruneContext] Kept {len(selected)}/{len(chunks)} chunks (~{used_tokens} tokens), dropped {dropped} low-relevance chunks")
    return selected

def call_with_pruned_context(
    context_chunks: list[ContextChunk],
    query: str,
    token_budget: int = 2000,
    system: str = "",
) -> str:
    relevant = select_chunks_by_budget(context_chunks, token_budget, query)

    context_text = "\n\n---\n\n".join(
        f"[{c.source}] (relevance: {c.relevance_score:.2f})\n{c.text}"
        for c in relevant
    )

    client = anthropic.Anthropic()
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": f"Context:\n{context_text}\n\nQuestion: {query}"}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    return response.content[0].text

if __name__ == "__main__":
    chunks = [
        ContextChunk("Python is a high-level, interpreted programming language known for readability.", "intro"),
        ContextChunk("Django is a Python web framework that follows the MVC pattern and includes an ORM.", "django_info"),
        ContextChunk("NumPy provides efficient array operations and is the foundation of scientific computing in Python.", "numpy_info"),
        ContextChunk("Python's GIL (Global Interpreter Lock) limits true parallelism for CPU-bound threads.", "gil_info"),
        ContextChunk("Flask is a micro web framework for Python with minimal built-in features.", "flask_info"),
        ContextChunk("asyncio enables concurrent I/O operations in Python via coroutines and event loops.", "asyncio_info"),
        ContextChunk("Python packaging is managed via pip and virtualenv or conda for environments.", "packaging_info"),
        ContextChunk("The walrus operator := was introduced in Python 3.8 for assignment expressions.", "walrus_info"),
    ]

    result = call_with_pruned_context(
        chunks,
        query="How does Python handle concurrent web requests?",
        token_budget=500,
    )
    print(result)

# Expected Token Savings: 30-80% depending on corpus size; quality improves as noise is removed
# Environment: pip install anthropic
```

## Comparison

| Option | Mechanism | Extra API Cost | Latency Impact | Best For |
|--------|-----------|---------------|---------------|----------|
| 1. Rule-Based | Regex/heuristics | None | None | Fast, deterministic cleanup |
| 2. LLM-Based | Haiku rewrite | Small (once) | +1 Haiku RTT | High-value repeated prompts |
| 3. Sliding Window | Rolling summaries | Periodic | Async | Long multi-turn conversations |
| 4. Structured Pruning | Field filtering | None | None | JSON/API tool results |
| 5. Async Pipeline | Parallel compression | Haiku per chunk | Zero (parallel) | Multi-tool result pipelines |
| 6. Importance Pruning | Relevance scoring | None | None | Large RAG context windows |
