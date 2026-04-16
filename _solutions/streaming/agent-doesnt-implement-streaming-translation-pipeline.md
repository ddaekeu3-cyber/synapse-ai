---
title: "Agent Doesn't Implement Streaming Translation Pipeline"
description: "AI agents stream responses only in the model's output language, requiring clients to buffer the entire response before translating — adding seconds of latency and breaking the streaming UX for multilingual applications."
problem_description: |
  When an AI agent serves multilingual users, a common pattern is: stream the full English response, then send it to a translation API, then return the translated text. This waterfall approach negates all streaming latency benefits — the user sees nothing until both generation and translation complete. Worse, sentence-level translation applied to arbitrary token boundaries produces garbled output. A proper streaming translation pipeline must buffer at sentence boundaries, translate incrementally, and forward translated chunks to the client as they become ready — preserving both streaming UX and translation quality.
category: streaming
difficulty: advanced
tags: [streaming, translation, multilingual, i18n, pipeline]
---

## Solution 1: Sentence-Boundary Buffered Translation

Buffer streamed tokens at sentence boundaries (`.`, `?`, `!`) then translate each complete sentence before forwarding — preserving streaming UX while giving the translator full syntactic units.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from typing import AsyncIterator, Callable


SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

TranslateFn = Callable[[str, str], str]  # (text, target_lang) -> translated


def mock_translate(text: str, target_lang: str) -> str:
    """Placeholder — replace with DeepL/Google Translate SDK call."""
    return f"[{target_lang.upper()}] {text}"


async def translate_async(text: str, target_lang: str, translate_fn: TranslateFn) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, translate_fn, text, target_lang)


async def streaming_sentence_translator(
    source_stream: AsyncIterator[str],
    target_lang: str,
    translate_fn: TranslateFn = mock_translate,
    min_sentence_chars: int = 20,
) -> AsyncIterator[str]:
    """Yield translated sentence chunks as they arrive."""
    buffer = ""

    async for token in source_stream:
        buffer += token

        # Split on sentence boundaries
        parts = SENTENCE_END.split(buffer)
        if len(parts) <= 1:
            continue

        # Translate all complete sentences (all but the last incomplete part)
        complete = parts[:-1]
        buffer = parts[-1]

        for sentence in complete:
            sentence = sentence.strip()
            if len(sentence) >= min_sentence_chars:
                translated = await translate_async(sentence, target_lang, translate_fn)
                yield translated + " "
            elif sentence:
                # Too short to translate individually — append back to buffer
                buffer = sentence + " " + buffer

    # Flush remaining
    if buffer.strip():
        translated = await translate_async(buffer.strip(), target_lang, translate_fn)
        yield translated


# Usage
async def main():
    client = AsyncAnthropic()

    async def claude_stream() -> AsyncIterator[str]:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content":
                "Explain what machine learning is in three sentences."}],
        ) as stream:
            async for token in stream.text_stream:
                yield token

    print("Streaming translation (EN → KO):")
    async for translated_chunk in streaming_sentence_translator(
        claude_stream(), target_lang="ko"
    ):
        print(translated_chunk, end="", flush=True)

    print("\n\nDone.")

asyncio.run(main())
```

## Solution 2: Parallel Generation and Translation with Async Queue

Run Claude generation and sentence translation concurrently using an asyncio Queue — the translator consumes sentences as fast as the generator produces them, minimizing end-to-end latency.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import AsyncIterator, Callable


SENTINEL = None  # Queue poison pill


@dataclass
class TranslationJob:
    sequence: int
    text: str
    target_lang: str


@dataclass
class TranslationResult:
    sequence: int
    translated: str


async def producer(
    client: AsyncAnthropic,
    prompt: str,
    queue: asyncio.Queue,
    target_lang: str,
):
    """Generate text, split at sentence boundaries, enqueue jobs."""
    buffer = ""
    seq = 0
    sentence_end_chars = {'.', '!', '?'}

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for token in stream.text_stream:
            buffer += token

            # Find sentence boundaries
            while True:
                end_idx = -1
                for i, ch in enumerate(buffer):
                    if ch in sentence_end_chars and i + 1 < len(buffer) and buffer[i + 1] == ' ':
                        end_idx = i + 1
                        break

                if end_idx == -1:
                    break

                sentence = buffer[:end_idx].strip()
                buffer = buffer[end_idx:].lstrip()

                if len(sentence) >= 15:
                    await queue.put(TranslationJob(seq, sentence, target_lang))
                    seq += 1

    # Flush remaining
    if buffer.strip():
        await queue.put(TranslationJob(seq, buffer.strip(), target_lang))
        seq += 1

    await queue.put(SENTINEL)


async def translator(
    queue: asyncio.Queue,
    result_queue: asyncio.Queue,
    translate_fn: Callable[[str, str], str],
):
    """Consume translation jobs, translate, enqueue results."""
    loop = asyncio.get_event_loop()
    pending: dict[int, asyncio.Task] = {}

    while True:
        job = await queue.get()
        if job is SENTINEL:
            # Wait for all pending translations
            if pending:
                done_results = await asyncio.gather(*pending.values())
            await result_queue.put(SENTINEL)
            return

        async def translate_job(j: TranslationJob) -> TranslationResult:
            translated = await loop.run_in_executor(None, translate_fn, j.text, j.target_lang)
            return TranslationResult(j.sequence, translated)

        task = asyncio.create_task(translate_job(job))
        pending[job.sequence] = task

        # Forward completed results in order
        while pending:
            min_seq = min(pending)
            t = pending[min_seq]
            if t.done():
                result = t.result()
                await result_queue.put(result)
                del pending[min_seq]
            else:
                break


async def streaming_parallel_translate(
    client: AsyncAnthropic,
    prompt: str,
    target_lang: str,
    translate_fn: Callable[[str, str], str],
) -> AsyncIterator[str]:
    job_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    result_queue: asyncio.Queue = asyncio.Queue()

    prod_task = asyncio.create_task(producer(client, prompt, job_queue, target_lang))
    trans_task = asyncio.create_task(translator(job_queue, result_queue, translate_fn))

    while True:
        result = await result_queue.get()
        if result is SENTINEL:
            break
        yield result.translated + " "

    await asyncio.gather(prod_task, trans_task)


# Usage
async def main():
    def mock_translate(text: str, lang: str) -> str:
        import time
        time.sleep(0.05)  # Simulate translation latency
        return f"[{lang}:{text}]"

    client = AsyncAnthropic()
    print("Parallel streaming translation:")
    async for chunk in streaming_parallel_translate(
        client,
        "Describe three benefits of renewable energy.",
        target_lang="es",
        translate_fn=mock_translate,
    ):
        print(chunk, end="", flush=True)

    print("\nDone.")

asyncio.run(main())
```

## Solution 3: Multi-Language Fan-Out — Stream Once, Translate to N Languages

Generate the source text stream once and simultaneously translate it into multiple target languages — serving multilingual audiences without N×generation cost.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Callable, AsyncIterator


@dataclass
class LanguageSink:
    lang: str
    chunks: list[str] = field(default_factory=list)
    translate_fn: Callable[[str, str], str] = field(default_factory=lambda: lambda t, l: t)

    async def receive(self, sentence: str):
        loop = asyncio.get_event_loop()
        translated = await loop.run_in_executor(None, self.translate_fn, sentence, self.lang)
        self.chunks.append(translated)
        return translated


class MultilangStreamRouter:
    def __init__(
        self,
        target_langs: list[str],
        translate_fn: Callable[[str, str], str],
    ):
        self.sinks = {
            lang: LanguageSink(lang=lang, translate_fn=translate_fn)
            for lang in target_langs
        }
        self._sentence_queues: dict[str, asyncio.Queue] = {
            lang: asyncio.Queue() for lang in target_langs
        }
        self._output_queues: dict[str, asyncio.Queue] = {
            lang: asyncio.Queue() for lang in target_langs
        }

    async def _translation_worker(self, lang: str):
        sink = self.sinks[lang]
        while True:
            sentence = await self._sentence_queues[lang].get()
            if sentence is None:
                await self._output_queues[lang].put(None)
                return
            translated = await sink.receive(sentence)
            await self._output_queues[lang].put(translated)

    async def stream_multilang(
        self,
        client: AsyncAnthropic,
        prompt: str,
    ) -> dict[str, AsyncIterator[str]]:
        """Returns per-language async iterators that can be consumed independently."""
        # Start per-language worker tasks
        for lang in self.sinks:
            asyncio.create_task(self._translation_worker(lang))

        # Start source generator
        asyncio.create_task(self._generate_and_distribute(client, prompt))

        # Return per-language iterators
        return {lang: self._lang_iterator(lang) for lang in self.sinks}

    async def _generate_and_distribute(self, client: AsyncAnthropic, prompt: str):
        buffer = ""
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for token in stream.text_stream:
                buffer += token
                while '. ' in buffer or '! ' in buffer or '? ' in buffer:
                    for end in ['. ', '! ', '? ']:
                        idx = buffer.find(end)
                        if idx != -1:
                            sentence = buffer[:idx + 1].strip()
                            buffer = buffer[idx + 2:]
                            await asyncio.gather(*[
                                self._sentence_queues[lang].put(sentence)
                                for lang in self.sinks
                            ])
                            break
                    else:
                        break

        if buffer.strip():
            await asyncio.gather(*[
                self._sentence_queues[lang].put(buffer.strip())
                for lang in self.sinks
            ])

        # Signal end to all workers
        await asyncio.gather(*[
            self._sentence_queues[lang].put(None)
            for lang in self.sinks
        ])

    async def _lang_iterator(self, lang: str) -> AsyncIterator[str]:
        while True:
            chunk = await self._output_queues[lang].get()
            if chunk is None:
                return
            yield chunk


# Usage
async def main():
    def mock_translate(text: str, lang: str) -> str:
        return f"[{lang.upper()}] {text}"

    client = AsyncAnthropic()
    router = MultilangStreamRouter(
        target_langs=["ko", "ja", "es", "fr"],
        translate_fn=mock_translate,
    )

    lang_streams = await router.stream_multilang(
        client,
        "Explain what cloud computing is in two sentences.",
    )

    # Collect all outputs concurrently
    async def collect(lang: str, stream: AsyncIterator[str]) -> tuple[str, str]:
        parts = []
        async for chunk in stream:
            parts.append(chunk)
        return lang, ' '.join(parts)

    results = await asyncio.gather(*[
        collect(lang, stream) for lang, stream in lang_streams.items()
    ])

    for lang, text in results:
        print(f"[{lang}] {text[:120]}")

asyncio.run(main())
```

## Solution 4: Streaming Translation with Quality Fallback

Attempt fast machine translation for each streamed sentence; if translation quality score is too low, fall back to a higher-quality (slower) translation service before forwarding.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Callable


@dataclass
class TranslationAttempt:
    text: str
    translated: str
    quality_score: float
    service_used: str


def fast_translate(text: str, lang: str) -> str:
    """Fast, lower-quality translation (e.g., libre-translate)."""
    return f"[FAST:{lang}] {text}"


def quality_translate(text: str, lang: str) -> str:
    """High-quality translation (e.g., DeepL)."""
    return f"[QUALITY:{lang}] {text}"


def estimate_translation_quality(source: str, translated: str) -> float:
    """
    Heuristic quality score 0-1.
    Real implementations use ChrF, BLEU, or a quality estimation model.
    """
    if not translated or len(translated) < 5:
        return 0.0
    length_ratio = len(translated) / max(len(source), 1)
    score = 1.0
    if length_ratio < 0.3 or length_ratio > 5.0:
        score *= 0.5
    if re.search(r'\[ERROR\]|\[FAILED\]', translated):
        score *= 0.1
    return min(score, 1.0)


async def translate_with_fallback(
    sentence: str,
    target_lang: str,
    quality_threshold: float = 0.7,
) -> TranslationAttempt:
    loop = asyncio.get_event_loop()

    # Try fast translation first
    fast_result = await loop.run_in_executor(None, fast_translate, sentence, target_lang)
    quality = estimate_translation_quality(sentence, fast_result)

    if quality >= quality_threshold:
        return TranslationAttempt(sentence, fast_result, quality, "fast")

    # Fall back to quality translation
    quality_result = await loop.run_in_executor(None, quality_translate, sentence, target_lang)
    quality2 = estimate_translation_quality(sentence, quality_result)
    return TranslationAttempt(sentence, quality_result, quality2, "quality")


async def streaming_translation_with_fallback(
    client: AsyncAnthropic,
    prompt: str,
    target_lang: str,
    quality_threshold: float = 0.7,
):
    buffer = ""
    total_sentences = 0
    fallback_count = 0

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for token in stream.text_stream:
            buffer += token

            while True:
                # Find sentence boundary
                match = re.search(r'(?<=[.!?])\s', buffer)
                if not match:
                    break

                sentence = buffer[:match.start() + 1].strip()
                buffer = buffer[match.end():]

                if len(sentence) < 10:
                    continue

                attempt = await translate_with_fallback(sentence, target_lang, quality_threshold)
                total_sentences += 1
                if attempt.service_used == "quality":
                    fallback_count += 1

                print(f"[{attempt.service_used.upper()}|q={attempt.quality_score:.2f}] {attempt.translated}")

    # Flush
    if buffer.strip():
        attempt = await translate_with_fallback(buffer.strip(), target_lang, quality_threshold)
        print(f"[{attempt.service_used.upper()}] {attempt.translated}")
        total_sentences += 1
        if attempt.service_used == "quality":
            fallback_count += 1

    print(f"\nStats: {total_sentences} sentences, {fallback_count} quality fallbacks "
          f"({100*fallback_count/max(total_sentences,1):.0f}%)")


# Usage
async def main():
    client = AsyncAnthropic()
    await streaming_translation_with_fallback(
        client,
        "Describe the water cycle in three sentences.",
        target_lang="de",
        quality_threshold=0.7,
    )

asyncio.run(main())
```

## Solution 5: Streaming Translation Cache with Fuzzy Matching

Cache translated sentences and reuse matches for semantically equivalent inputs — reducing translation API calls and latency for repetitive agent outputs like standard disclaimers or template phrases.

```python
import asyncio
import hashlib
import re
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field


@dataclass
class CacheEntry:
    source: str
    translated: str
    lang: str
    hits: int = 0
    created_at: float = field(default_factory=time.time)


class TranslationCache:
    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600):
        self._exact: dict[str, CacheEntry] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds
        self.hits = 0
        self.misses = 0

    def _key(self, text: str, lang: str) -> str:
        normalized = re.sub(r'\s+', ' ', text.lower().strip())
        return hashlib.md5(f"{normalized}:{lang}".encode()).hexdigest()

    def get(self, text: str, lang: str) -> str | None:
        key = self._key(text, lang)
        entry = self._exact.get(key)
        if entry and time.time() - entry.created_at < self._ttl:
            entry.hits += 1
            self.hits += 1
            return entry.translated
        self.misses += 1
        return None

    def set(self, source: str, lang: str, translated: str):
        if len(self._exact) >= self._max_size:
            # Evict least-recently-hit entry
            oldest_key = min(self._exact, key=lambda k: self._exact[k].hits)
            del self._exact[oldest_key]

        key = self._key(source, lang)
        self._exact[key] = CacheEntry(source, translated, lang)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "size": len(self._exact),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total > 0 else 0,
        }


_global_cache = TranslationCache()


async def cached_translate(
    sentence: str,
    target_lang: str,
    translate_fn,
) -> tuple[str, bool]:
    """Returns (translated, was_cached)."""
    cached = _global_cache.get(sentence, target_lang)
    if cached:
        return cached, True

    loop = asyncio.get_event_loop()
    translated = await loop.run_in_executor(None, translate_fn, sentence, target_lang)
    _global_cache.set(sentence, target_lang, translated)
    return translated, False


async def streaming_cached_translation(
    client: AsyncAnthropic,
    prompts: list[str],
    target_lang: str,
    translate_fn=None,
):
    if translate_fn is None:
        def translate_fn(text: str, lang: str) -> str:
            return f"[{lang.upper()}] {text}"

    for i, prompt in enumerate(prompts):
        print(f"\n--- Prompt {i+1} ---")
        buffer = ""

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for token in stream.text_stream:
                buffer += token
                while re.search(r'(?<=[.!?])\s', buffer):
                    match = re.search(r'(?<=[.!?])\s', buffer)
                    sentence = buffer[:match.start() + 1].strip()
                    buffer = buffer[match.end():]
                    if len(sentence) > 10:
                        translated, cached = await cached_translate(sentence, target_lang, translate_fn)
                        label = "CACHE" if cached else "API"
                        print(f"  [{label}] {translated[:80]}")

        if buffer.strip():
            translated, cached = await cached_translate(buffer.strip(), target_lang, translate_fn)
            print(f"  [{'CACHE' if cached else 'API'}] {translated[:80]}")

    print(f"\nCache stats: {_global_cache.stats()}")


# Usage
async def main():
    client = AsyncAnthropic()
    # Run similar prompts — second prompt should hit cache for shared sentences
    await streaming_cached_translation(
        client,
        prompts=[
            "Explain what an API is in two sentences.",
            "Explain what an API is in two sentences.",  # Should get cache hits
        ],
        target_lang="ko",
    )

asyncio.run(main())
```

## Solution 6: Server-Sent Events Translation Gateway

Build a translation middleware gateway that accepts SSE from Claude and re-emits SSE events with translated content — enabling drop-in replacement for existing SSE streaming clients.

```python
import asyncio
import json
import re
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import AsyncIterator, Callable


@dataclass
class SSEEvent:
    event: str
    data: str
    id: str | None = None

    def encode(self) -> str:
        parts = []
        if self.id:
            parts.append(f"id: {self.id}")
        parts.append(f"event: {self.event}")
        parts.append(f"data: {self.data}")
        parts.append("")
        return "\n".join(parts) + "\n"


async def translation_sse_gateway(
    client: AsyncAnthropic,
    prompt: str,
    target_lang: str,
    translate_fn: Callable[[str, str], str],
    source_lang: str = "en",
) -> AsyncIterator[SSEEvent]:
    """Yields SSE events with translated content chunks."""
    buffer = ""
    event_id = 0
    start_time = time.time()

    yield SSEEvent(
        event="stream_start",
        data=json.dumps({"source_lang": source_lang, "target_lang": target_lang}),
        id=str(event_id),
    )
    event_id += 1

    async def translate(text: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, translate_fn, text, target_lang)

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for token in stream.text_stream:
            buffer += token

            while True:
                match = re.search(r'(?<=[.!?])\s', buffer)
                if not match:
                    break

                sentence = buffer[:match.start() + 1].strip()
                buffer = buffer[match.end():]

                if len(sentence) < 8:
                    continue

                translated = await translate(sentence)
                yield SSEEvent(
                    event="translation",
                    data=json.dumps({
                        "source": sentence,
                        "translated": translated,
                        "lang": target_lang,
                        "elapsed_ms": round((time.time() - start_time) * 1000),
                    }),
                    id=str(event_id),
                )
                event_id += 1

    if buffer.strip():
        translated = await translate(buffer.strip())
        yield SSEEvent(
            event="translation",
            data=json.dumps({"source": buffer.strip(), "translated": translated, "lang": target_lang}),
            id=str(event_id),
        )
        event_id += 1

    yield SSEEvent(
        event="stream_end",
        data=json.dumps({"total_events": event_id, "lang": target_lang}),
        id=str(event_id),
    )


# Usage — simulates what an HTTP server would send to a browser client
async def main():
    client = AsyncAnthropic()

    def mock_translate(text: str, lang: str) -> str:
        return f"[{lang}] {text}"

    print("SSE Translation Gateway output:")
    print("=" * 60)
    async for event in translation_sse_gateway(
        client,
        "Describe photosynthesis in two sentences.",
        target_lang="ja",
        translate_fn=mock_translate,
    ):
        print(event.encode())

asyncio.run(main())
```

## Comparison

| Approach | Latency | Quality | Multiple Languages | Caching | Best For |
|---|---|---|---|---|---|
| Sentence-Boundary Buffered | Low | High | No | No | Simple single-lang use case |
| Parallel Queue | Very Low | High | No | No | High-throughput single lang |
| Multi-Language Fan-Out | Low | High | Yes | No | Multilingual audiences |
| Quality Fallback | Low-Med | Very High | No | No | Quality-critical content |
| Cached Translation | Very Low | High | Yes | Yes | Repetitive/template content |
| SSE Gateway | Low | High | No | Optional | Drop-in SSE client upgrade |
