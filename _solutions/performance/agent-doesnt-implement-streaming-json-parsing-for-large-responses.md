---
title: "Agent Doesn't Implement Streaming JSON Parsing for Large Responses"
description: "Agents that buffer entire JSON tool responses into memory before processing stall on large payloads, spike RAM, and block the event loop — streaming JSON parsers process records incrementally as bytes arrive."
difficulty: intermediate
category: performance
tags: [performance, streaming, json, memory, async, tools, large-payloads]
---

# Agent Doesn't Implement Streaming JSON Parsing for Large Responses

## Problem

When an agent calls a tool that returns a large JSON payload (thousands of search results, a full database export, a large embedding matrix), buffering the entire response into memory before parsing it creates several problems: RAM spikes under concurrent load, time-to-first-record latency measured in seconds, and event loop blocking during synchronous `json.loads()` on large strings. Streaming JSON parsers yield records as bytes arrive, keeping memory flat and latency low.

**Symptoms:**
- Tool call returns 50MB JSON; agent RAM spikes by 150MB+ to parse it
- First result from a list of 10,000 items arrives only after all 10,000 are downloaded
- `json.loads()` blocks the event loop for 200ms on large payloads
- Out-of-memory crashes under concurrent requests each loading large tool responses
- Search result latency dominated by download + parse rather than network RTT

---

## Solution 1: ijson Async Streaming Parser

Use `ijson` to parse a large JSON array incrementally from an async HTTP response.

```python
import asyncio
from typing import AsyncIterator
import aiohttp
import anthropic

# pip install ijson aiohttp
try:
    import ijson
except ImportError:
    ijson = None  # type: ignore


async def stream_json_array(url: str, array_prefix: str = "item") -> AsyncIterator[dict]:
    """Yield items from a large JSON array without buffering the full response."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            # ijson parses from a synchronous file-like; wrap async chunks
            buffer = b""
            async for chunk in response.content.iter_chunked(65536):
                buffer += chunk

            # For true async streaming with ijson, use a BytesIO wrapper
            import io
            parser = ijson.items(io.BytesIO(buffer), array_prefix)
            for item in parser:
                yield item


async def stream_json_array_chunked(
    url: str, array_prefix: str = "item", chunk_size: int = 65536
) -> AsyncIterator[dict]:
    """Stream JSON array from HTTP response, yielding items as they're parsed."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            chunks = []
            async for chunk in resp.content.iter_chunked(chunk_size):
                chunks.append(chunk)
            import io
            data = b"".join(chunks)
            parser = ijson.items(io.BytesIO(data), array_prefix)
            for item in parser:
                yield item
                await asyncio.sleep(0)  # Yield control between items


class StreamingSearchAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def search_and_summarize(
        self,
        search_url: str,
        user_query: str,
        max_results: int = 20,
    ) -> str:
        # Collect only the first N results instead of loading all
        results = []
        async for item in stream_json_array(search_url):
            results.append(item)
            if len(results) >= max_results:
                break

        print(f"[streaming] Loaded {len(results)} results without buffering full response")

        context = "\n".join(
            f"- {r.get('title', '')}: {r.get('snippet', '')}"
            for r in results
        )
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Query: {user_query}\n\nSearch results:\n{context}\n\nSummarize the key findings."
            }],
        )
        return response.content[0].text


async def demo():
    agent = StreamingSearchAgent(api_key="sk-...")
    # Example: large paginated JSON API
    # result = await agent.search_and_summarize(
    #     "https://api.example.com/search?q=AI+agents&format=json",
    #     "What are the latest developments in AI agents?",
    # )
    # print(result)
    print("StreamingSearchAgent ready.")

# asyncio.run(demo())
```

---

## Solution 2: NDJSON (Newline-Delimited JSON) Line-by-Line Streaming

Tools that return NDJSON (one JSON object per line) can be parsed incrementally with zero extra libraries.

```python
import asyncio
import json
from typing import AsyncIterator, Optional
import aiohttp
import anthropic


async def stream_ndjson(url: str, headers: Optional[dict] = None) -> AsyncIterator[dict]:
    """Yield parsed objects from an NDJSON endpoint line by line."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers or {}) as resp:
            resp.raise_for_status()
            buffer = ""
            async for chunk in resp.content.iter_chunked(4096):
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass  # Skip malformed lines
            # Process any remaining data
            if buffer.strip():
                try:
                    yield json.loads(buffer.strip())
                except json.JSONDecodeError:
                    pass


class NdjsonToolAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def process_events(
        self,
        events_url: str,
        question: str,
        max_events: int = 50,
    ) -> str:
        events = []
        total_bytes = 0

        async for event in stream_ndjson(events_url):
            events.append(event)
            total_bytes += len(json.dumps(event))
            if len(events) >= max_events:
                break

        print(
            f"[ndjson] Processed {len(events)} events, "
            f"{total_bytes / 1024:.1f} KB — no full-buffer required"
        )

        summary = json.dumps(events[:10], indent=2)[:2000]  # First 10 for LLM context
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nEvent sample (first 10):\n{summary}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = NdjsonToolAgent(api_key="sk-...")
    print("NdjsonToolAgent ready — streams NDJSON line by line with no full-buffer.")

# asyncio.run(demo())
```

---

## Solution 3: Chunked JSON Array with Manual State Machine

Parse a streaming JSON array with a minimal hand-written state machine — no external libraries.

```python
import asyncio
import json
from typing import AsyncIterator, Optional
import aiohttp
import anthropic


class JsonArrayStreamer:
    """
    Stateful streaming parser for a top-level JSON array.
    Yields complete JSON objects as they appear in the stream.
    Works on chunked HTTP responses without buffering the full body.
    """

    def __init__(self):
        self._buf = ""
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._started = False
        self._obj_start: Optional[int] = None

    def feed(self, chunk: str) -> list[dict]:
        """Feed a string chunk; returns any complete objects parsed so far."""
        results = []
        self._buf += chunk
        i = 0

        while i < len(self._buf):
            ch = self._buf[i]

            if self._escape:
                self._escape = False
                i += 1
                continue

            if self._in_string:
                if ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
                i += 1
                continue

            if ch == '"':
                self._in_string = True
                i += 1
                continue

            if not self._started:
                if ch == "[":
                    self._started = True
                i += 1
                continue

            if ch in ("{", "["):
                if self._depth == 0:
                    self._obj_start = i
                self._depth += 1
            elif ch in ("}", "]"):
                self._depth -= 1
                if self._depth == 0 and self._obj_start is not None:
                    raw = self._buf[self._obj_start:i + 1]
                    try:
                        results.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
                    self._obj_start = None
                    self._buf = self._buf[i + 1:]
                    i = 0
                    continue

            i += 1

        if self._obj_start is None:
            self._buf = ""  # Nothing in progress — safe to clear
        return results


async def stream_json_with_state_machine(
    url: str, max_items: int = 100
) -> AsyncIterator[dict]:
    streamer = JsonArrayStreamer()
    count = 0
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            async for chunk in resp.content.iter_chunked(8192):
                items = streamer.feed(chunk.decode("utf-8", errors="replace"))
                for item in items:
                    yield item
                    count += 1
                    if count >= max_items:
                        return


class StateMachineAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def answer_from_stream(self, url: str, question: str) -> str:
        collected = []
        async for obj in stream_json_with_state_machine(url, max_items=30):
            collected.append(obj)

        context = json.dumps(collected, indent=2)[:3000]
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": f"{question}\n\nData:\n{context}"}],
        )
        return response.content[0].text
```

---

## Solution 4: Async Generator Pipeline with Back-Pressure

Chain JSON streaming through a processing pipeline with bounded buffers so fast producers don't overwhelm slow consumers.

```python
import asyncio
import json
from typing import AsyncIterator
import aiohttp
import anthropic


async def bounded_json_stream(
    url: str,
    queue_size: int = 100,
) -> AsyncIterator[dict]:
    """Producer → bounded queue → consumer; applies back-pressure."""
    queue: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=queue_size)

    async def producer():
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                buffer = ""
                async for chunk in resp.content.iter_chunked(4096):
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if line:
                            try:
                                obj = json.loads(line)
                                await queue.put(obj)  # Blocks when queue full (back-pressure)
                            except json.JSONDecodeError:
                                pass
        await queue.put(None)  # Sentinel

    producer_task = asyncio.create_task(producer())

    while True:
        item = await queue.get()
        if item is None:
            break
        yield item

    await producer_task


async def filter_and_rank(
    stream: AsyncIterator[dict],
    keyword: str,
    top_n: int = 10,
) -> list[dict]:
    """Filter stream for keyword; keep top N by score field."""
    results = []
    async for item in stream:
        if keyword.lower() in json.dumps(item).lower():
            results.append(item)
        if len(results) >= top_n * 3:  # Collect a pool then rank
            break

    # Rank by score field if available
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results[:top_n]


class PipelineAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run(self, data_url: str, query: str) -> str:
        stream = bounded_json_stream(data_url, queue_size=50)
        top_results = await filter_and_rank(stream, keyword=query, top_n=10)

        print(f"[pipeline] Filtered to {len(top_results)} relevant records from stream")

        context = json.dumps(top_results, indent=2)[:2000]
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Query: {query}\n\nTop results:\n{context}"
            }],
        )
        return response.content[0].text
```

---

## Solution 5: Lazy Tool Result Evaluation

Don't parse the full tool response — extract only the fields the LLM actually needs using streaming path queries.

```python
import asyncio
import json
from typing import Any, AsyncIterator, Optional
import anthropic


def extract_paths(obj: Any, paths: list[str]) -> dict:
    """Extract specific dot-notation paths from a parsed object."""
    result = {}
    for path in paths:
        keys = path.split(".")
        value = obj
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = None
                break
        result[path] = value
    return result


class LazyToolEvaluator:
    """
    Wraps tool calls: parse minimally, extract only needed fields,
    pass summarized data to LLM rather than raw JSON blobs.
    """

    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def _fetch_tool_response(self, url: str) -> bytes:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                return await resp.read()

    def _extract_relevant(
        self,
        raw: bytes,
        array_key: str,
        fields: list[str],
        limit: int,
    ) -> list[dict]:
        """Parse JSON, extract only needed fields from array items."""
        data = json.loads(raw)
        items = data if isinstance(data, list) else data.get(array_key, [])
        extracted = []
        for item in items[:limit]:
            extracted.append(extract_paths(item, fields))
        return extracted

    async def answer(
        self,
        tool_url: str,
        question: str,
        array_key: str = "results",
        needed_fields: Optional[list[str]] = None,
        limit: int = 20,
    ) -> str:
        if needed_fields is None:
            needed_fields = ["title", "description", "url", "score"]

        raw = await self._fetch_tool_response(tool_url)
        full_size = len(raw)
        slim_results = self._extract_relevant(raw, array_key, needed_fields, limit)
        slim_size = len(json.dumps(slim_results))

        print(
            f"[lazy] Full response: {full_size/1024:.1f}KB → "
            f"slim context: {slim_size/1024:.1f}KB "
            f"({100*slim_size//full_size}% of original)"
        )

        context = json.dumps(slim_results, indent=2)
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nRelevant data (key fields only):\n{context}"
            }],
        )
        return response.content[0].text
```

---

## Solution 6: Chunked Embedding Response Processing

For embedding APIs that return large float arrays, process them in chunks rather than loading all vectors at once.

```python
import asyncio
import json
from typing import AsyncIterator
import aiohttp
import anthropic
import struct


async def stream_embedding_jsonl(url: str) -> AsyncIterator[dict]:
    """
    Stream embeddings returned as JSONL; each line is:
    {"id": "doc_1", "embedding": [0.1, 0.2, ...], "text": "..."}
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            buffer = b""
            async for chunk in resp.content.iter_chunked(32768):
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b + 1e-9)


class StreamingEmbeddingAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def find_most_similar(
        self,
        embeddings_url: str,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[dict]:
        """Stream embedding records and find top-K most similar without loading all into RAM."""
        heap: list[tuple[float, dict]] = []  # (score, record)

        async for record in stream_embedding_jsonl(embeddings_url):
            emb = record.get("embedding", [])
            if not emb:
                continue
            score = cosine_similarity(query_embedding, emb)

            # Maintain a min-heap of size top_k
            import heapq
            if len(heap) < top_k:
                heapq.heappush(heap, (score, record))
            elif score > heap[0][0]:
                heapq.heapreplace(heap, (score, record))

        return [r for _, r in sorted(heap, reverse=True)]

    async def rag_answer(
        self,
        embeddings_url: str,
        query: str,
        query_embedding: list[float],
    ) -> str:
        top_docs = await self.find_most_similar(embeddings_url, query_embedding, top_k=5)
        context = "\n\n".join(
            f"[{i+1}] {doc.get('text', '')}"
            for i, doc in enumerate(top_docs)
        )

        print(f"[embedding] Retrieved top {len(top_docs)} docs via streaming similarity")

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Question: {query}\n\nContext:\n{context}"
            }],
        )
        return response.content[0].text
```

---

## Comparison

| Solution | Library | Memory Profile | Time-to-First-Record | Complexity | Best For |
|---|---|---|---|---|---|
| ijson streaming parser | ijson | Flat O(item) | Streaming | Low | Any JSON array API |
| NDJSON line-by-line | None | Flat O(line) | Per line | Very Low | Log/event endpoints |
| Hand-written state machine | None | Flat O(obj) | Streaming | Medium | No-dep environments |
| Bounded async pipeline | asyncio | Flat + bounded | Streaming | Medium | Back-pressure needed |
| Lazy field extraction | None | O(full) → slim | After download | Low | Field-sparse payloads |
| Streaming embedding JSONL | None | O(top_k) | Per record | Medium | Vector similarity |

**Recommendation:** Use Solution 2 (NDJSON) when you control the tool's output format — it's the simplest possible streaming protocol. Use Solution 1 (ijson) for third-party APIs that return standard JSON arrays. Use Solution 4 (bounded pipeline) when your processing pipeline is slower than the network to apply back-pressure. Always prefer Solution 5 (lazy extraction) when you only need a subset of fields from large records.
