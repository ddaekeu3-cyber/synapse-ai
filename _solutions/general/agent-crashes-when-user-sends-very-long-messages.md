---
layout: solution
title: "Agent Crashes When User Sends Very Long Messages"
category: general
description: "Agent throws a context window exceeded error or truncates silently when users paste very long documents, code files, or transcripts directly into the chat."
tags: [general, context-window, chunking, truncation, preprocessing, streaming]
---

## Symptom

Users paste a 50-page PDF or a 10,000-line log file into the chat and the agent either crashes:

```
anthropic.BadRequestError: prompt is too long:
  200,000 tokens > 200,000 token limit
```

Or silently truncates the input, answering based on only the first portion of the document — without warning the user.

## Root Cause

The agent passes user input directly to the API without checking input length. Long messages exceed the model's context window. Even within the limit, very long inputs leave insufficient room for the response, or push prior conversation history out of context.

## Fix

---

### Option 1 — Pre-Flight Length Check with Graceful Rejection

Before sending to the API, estimate the token count and reject inputs that are too long with a clear error message and suggestions for the user.

```python
import anthropic

client = anthropic.Anthropic()

# Approximate token estimation (1 token ≈ 4 characters for English)
def estimate_tokens(text: str) -> int:
    return len(text) // 4

MAX_USER_INPUT_TOKENS = 50_000   # Reserve the rest for system + history + response
MAX_RESPONSE_TOKENS = 4_096

def chat_with_length_guard(
    messages: list[dict],
    user_message: str,
    system_prompt: str = "You are a helpful assistant.",
) -> str:
    input_tokens = estimate_tokens(user_message)

    if input_tokens > MAX_USER_INPUT_TOKENS:
        char_limit = MAX_USER_INPUT_TOKENS * 4
        return (
            f"Your message is too long (~{input_tokens:,} tokens estimated). "
            f"Please keep it under {MAX_USER_INPUT_TOKENS:,} tokens "
            f"({char_limit:,} characters).\n\n"
            "**Suggestions:**\n"
            "- Paste only the relevant section of the document\n"
            "- Ask me to analyse specific parts separately\n"
            "- Upload the file via a document URL if your interface supports it"
        )

    # Also check total context budget
    history_tokens = sum(estimate_tokens(m["content"]) for m in messages
                         if isinstance(m.get("content"), str))
    system_tokens = estimate_tokens(system_prompt)
    total = system_tokens + history_tokens + input_tokens + MAX_RESPONSE_TOKENS

    MODEL_LIMIT = 200_000
    if total > MODEL_LIMIT:
        return (
            f"The conversation is getting too long ({total:,} tokens total). "
            "Please start a new session or summarise the discussion so far."
        )

    messages = messages + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=messages,
    )

    return response.content[0].text

# Usage
history = []

# Normal message — passes
reply = chat_with_length_guard(history, "What is 2 + 2?")
print(reply)

# Simulated very long message
very_long = "word " * 60_000  # ~300K chars → ~75K tokens
reply = chat_with_length_guard(history, very_long)
print(reply)
```

**Expected Token Savings:** Prevents failed API calls entirely; saves wasted token spend on rejected inputs
**Environment:** `pip install anthropic`

---

### Option 2 — Automatic Chunking with Per-Chunk Summarisation

Split long input into chunks, summarise each chunk, then synthesise the summaries. The user gets an answer that covers the full document without the full document ever touching the context window at once.

```python
import anthropic

client = anthropic.Anthropic()

CHUNK_SIZE_CHARS = 12_000   # ~3K tokens per chunk
CHUNK_OVERLAP_CHARS = 500   # Overlap to preserve sentence continuity

def split_into_chunks(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE_CHARS, len(text))
        # Try to break at sentence boundary
        if end < len(text):
            last_period = text.rfind(".", start, end)
            if last_period > start + CHUNK_SIZE_CHARS // 2:
                end = last_period + 1
        chunks.append(text[start:end].strip())
        start = end - CHUNK_OVERLAP_CHARS
    return [c for c in chunks if c]

def summarise_chunk(chunk: str, question: str, chunk_index: int, total: int) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=(
            "You are a precise document analyst. "
            "Extract only the information relevant to the user's question. "
            "Be concise."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Document section {chunk_index + 1}/{total}:\n\n{chunk}\n\n"
                f"Question: {question}\n\n"
                "Extract only the parts of this section relevant to the question. "
                "If nothing is relevant, say 'No relevant content in this section.'"
            ),
        }],
    )
    return response.content[0].text

def synthesise(summaries: list[str], question: str) -> str:
    combined = "\n\n---\n\n".join(
        f"Section {i + 1} notes:\n{s}" for i, s in enumerate(summaries)
        if "No relevant content" not in s
    )

    if not combined:
        return "No relevant information was found in the document for your question."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a helpful assistant. Synthesise the provided section notes into a clear, complete answer.",
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nSection notes:\n{combined}",
        }],
    )
    return response.content[0].text

def answer_long_document(document: str, question: str) -> str:
    if len(document) <= CHUNK_SIZE_CHARS:
        # Short enough to handle directly
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": f"{document}\n\nQuestion: {question}"}],
        )
        return response.content[0].text

    chunks = split_into_chunks(document)
    print(f"Split into {len(chunks)} chunks")

    summaries = [
        summarise_chunk(chunk, question, i, len(chunks))
        for i, chunk in enumerate(chunks)
    ]

    return synthesise(summaries, question)

# Simulate a long document
long_doc = ("This is paragraph content about various topics. " * 200 + "\n") * 20
answer = answer_long_document(long_doc, "What is the main theme of this document?")
print(answer)
```

**Expected Token Savings:** ~60% vs sending full document — only relevant extracts reach synthesis
**Environment:** `pip install anthropic`

---

### Option 3 — Smart Truncation with User Warning

If the message is too long but truncation is acceptable, truncate intelligently (preserve beginning and end, remove middle) and warn the user clearly.

```python
import anthropic

client = anthropic.Anthropic()

MAX_CHARS = 40_000   # ~10K tokens
KEEP_START = 20_000  # Preserve first 20K chars (context)
KEEP_END = 18_000    # Preserve last 18K chars (most recent/relevant)

def smart_truncate(text: str) -> tuple[str, bool, int]:
    """
    Returns (truncated_text, was_truncated, removed_char_count).
    Keeps the beginning and end — removes the middle.
    """
    if len(text) <= MAX_CHARS:
        return text, False, 0

    removed = len(text) - KEEP_START - KEEP_END
    truncated = (
        text[:KEEP_START]
        + f"\n\n[... {removed:,} characters omitted for length ...]\n\n"
        + text[-KEEP_END:]
    )
    return truncated, True, removed

def chat_with_smart_truncation(user_message: str) -> str:
    truncated, was_truncated, removed = smart_truncate(user_message)

    warning = ""
    if was_truncated:
        warning = (
            f"> **Note:** Your message was {len(user_message):,} characters long. "
            f"The middle {removed:,} characters were omitted to fit the context window. "
            "If the omitted section is important, please paste it separately.\n\n"
        )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": truncated}],
    )

    return warning + response.content[0].text

# Simulate a very long paste
long_input = (
    "START OF DOCUMENT. Important setup information here.\n"
    + "This is middle filler content. " * 5000
    + "\nEND OF DOCUMENT. Final conclusion goes here."
)

result = chat_with_smart_truncation(long_input)
print(result[:500])
```

**Expected Token Savings:** Reduces input tokens proportionally to truncation amount
**Environment:** `pip install anthropic`

---

### Option 4 — Streaming with Early Abort on Context Overflow

Use streaming so the agent starts responding immediately. If a context overflow error occurs mid-stream, emit a graceful error message rather than crashing.

```python
import anthropic
from anthropic import APIStatusError

client = anthropic.Anthropic()

def stream_with_overflow_guard(
    user_message: str,
    max_input_chars: int = 800_000,
) -> str:
    """
    Streams the response. If input is too long, returns a user-friendly message.
    """
    if len(user_message) > max_input_chars:
        approx_tokens = len(user_message) // 4
        return (
            f"Your input is approximately {approx_tokens:,} tokens, "
            f"which exceeds what I can process at once.\n\n"
            "**Options:**\n"
            "1. Break your content into smaller sections and ask about each separately\n"
            "2. Paste only the specific portion you need help with\n"
            "3. Describe what you need rather than pasting the full content"
        )

    collected = []
    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            for text in stream.text_stream:
                collected.append(text)
                print(text, end="", flush=True)
        print()
        return "".join(collected)

    except APIStatusError as e:
        if "prompt is too long" in str(e) or e.status_code == 400:
            partial = "".join(collected)
            error_msg = (
                "\n\n[Your message was too long for the model to process. "
                "Please shorten it and try again.]"
            )
            return partial + error_msg if partial else error_msg.strip("[]")
        raise

# Test with a manageable message
result = stream_with_overflow_guard("Summarise the key principles of clean code.")
print(f"\nFull response length: {len(result)}")
```

**Expected Token Savings:** Fails fast on oversized inputs; no wasted inference
**Environment:** `pip install anthropic`

---

### Option 5 — Sliding Window Context Management

Maintain a rolling window of conversation history. When history grows too large, summarise older turns and replace them with the summary — preserving meaning while reclaiming context space.

```python
import anthropic

client = anthropic.Anthropic()

MAX_HISTORY_CHARS = 30_000
SUMMARISE_THRESHOLD = 24_000  # Trigger summarisation before hitting the limit

def history_size(messages: list[dict]) -> int:
    return sum(
        len(m["content"]) if isinstance(m["content"], str) else 0
        for m in messages
    )

def summarise_old_messages(messages: list[dict], keep_last: int = 4) -> list[dict]:
    """
    Summarise all but the last `keep_last` messages into a single summary message.
    """
    if len(messages) <= keep_last:
        return messages

    to_summarise = messages[:-keep_last]
    to_keep = messages[-keep_last:]

    conversation_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in to_summarise
        if isinstance(m.get("content"), str)
    )

    summary_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="Summarise the following conversation concisely, preserving all key facts and decisions.",
        messages=[{"role": "user", "content": conversation_text}],
    )

    summary = summary_response.content[0].text

    summary_message = {
        "role": "user",
        "content": f"[CONVERSATION SUMMARY — {len(to_summarise)} earlier turns condensed]\n{summary}",
    }
    placeholder = {
        "role": "assistant",
        "content": "I have the summary of our earlier conversation.",
    }

    return [summary_message, placeholder] + to_keep

def chat_with_sliding_window(
    history: list[dict],
    user_message: str,
) -> tuple[str, list[dict]]:
    # Compress history if needed before adding new message
    if history_size(history) > SUMMARISE_THRESHOLD:
        print("[INFO] Compressing conversation history...")
        history = summarise_old_messages(history)

    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=history,
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply, history

# Simulate a long conversation
conversation = []
for i in range(20):
    msg = f"Turn {i}: " + "This is a long message with lots of content. " * 100
    reply, conversation = chat_with_sliding_window(conversation, msg)
    print(f"Turn {i}: history size = {history_size(conversation):,} chars")
```

**Expected Token Savings:** ~70% on history tokens after compression events
**Environment:** `pip install anthropic`

---

### Option 6 — Selective Extraction Before Sending

Use a Haiku pre-processor to extract only the relevant portions of a long document before sending to the main model. The main model never sees irrelevant content.

```python
import anthropic

client = anthropic.Anthropic()

MAX_DIRECT_CHARS = 10_000

def extract_relevant_content(document: str, question: str) -> str:
    """Use a cheap model to extract only what's needed."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=(
            "You are a document extraction assistant. "
            "Extract only the sections of the document that are directly relevant "
            "to answering the user's question. Quote verbatim. "
            "If the whole document is relevant, say so and return up to 8000 characters."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Document (first 80K chars shown):\n{document[:80_000]}"
            ),
        }],
    )
    return response.content[0].text

def answer_with_extraction(document: str, question: str) -> str:
    if len(document) <= MAX_DIRECT_CHARS:
        # Short enough — send directly
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": f"Document:\n{document}\n\nQuestion: {question}",
            }],
        )
        return response.content[0].text

    print(f"Document is {len(document):,} chars — extracting relevant content first...")
    relevant = extract_relevant_content(document, question)
    print(f"Extracted {len(relevant):,} chars")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Answer the question based on the provided relevant document excerpts.",
        messages=[{
            "role": "user",
            "content": f"Relevant excerpts:\n{relevant}\n\nQuestion: {question}",
        }],
    )
    return response.content[0].text

# Simulate a large document
large_doc = (
    "Chapter 1: Introduction\nThis chapter introduces the topic.\n\n"
    + "Filler paragraph content. " * 500
    + "\n\nChapter 5: Conclusion\nThe main finding is that extraction saves tokens.\n"
    + "More filler. " * 500
)

answer = answer_with_extraction(large_doc, "What is the main finding?")
print(answer)
```

**Expected Token Savings:** ~85% on main model input tokens — only ~15% of document reaches Sonnet
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Approach | User Transparency | Token Savings | Best For |
|--------|----------|-------------------|---------------|----------|
| Pre-flight Rejection | Hard limit | High | 100% (fail fast) | Simple bots with clear limits |
| Chunking + Summarise | MapReduce | Medium | ~60% | Long document Q&A |
| Smart Truncation | Start+End keep | High (with warning) | Proportional | Log/transcript analysis |
| Stream + Guard | Reactive | Medium | Fail-fast | Interactive chat |
| Sliding Window | History compression | Low | ~70% on history | Long multi-turn sessions |
| Selective Extraction | Pre-filter | Low | ~85% | Domain-specific Q&A |

**Recommended starting point:** Option 1 (Pre-flight Rejection) for user-facing apps; Option 2 (Chunking) for document processing pipelines.
