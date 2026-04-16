---
title: "Agent Doesn't Implement Flash-Attention-Aware Context Chunking"
description: "AI agents that pass arbitrarily long contexts to LLMs without chunking hit quadratic attention memory and latency walls. Flash-attention processes context in tiles that fit in SRAM, making memory linear in sequence length — but only if the agent feeds context in tile-aligned chunks, avoids padding waste, and packs multiple short documents into the same chunk rather than issuing separate requests."
date: 2025-02-10
difficulty: advanced
category: performance
slug: agent-doesnt-implement-flash-attention-aware-context-chunking
tags:
  - flash-attention
  - context-chunking
  - long-context
  - tiling
  - token-packing
  - latency
  - throughput
  - performance
symptoms:
  - "LLM inference time grows quadratically as context length doubles"
  - "Agent sends 10 short documents as 10 separate requests instead of one packed request"
  - "Context window is padded to the nearest power-of-two, wasting 30-40% of tokens"
  - "Agent naively concatenates all retrieved chunks without considering attention tile boundaries"
  - "GPU OOM errors on long-context calls that should fit within published max_tokens"
---

## Problem

Standard attention is O(n²) in memory and time. Flash-attention tiles the Q·K·V computation into SRAM-sized blocks, reducing memory to O(n) — but the tile size (typically 64–256 tokens) means the agent should avoid straddling document boundaries at tile edges, pack multiple queries into one forward pass, and never issue a padded request when packing would eliminate the padding. Agents that ignore tiling leave throughput and memory savings on the table.

---

## Solution 1: TileAlignedChunker — Chunk Documents at Tile Boundaries

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DocumentChunk:
    doc_id: str
    chunk_index: int
    tokens: List[int]
    token_count: int
    tile_aligned: bool


class TileAlignedChunker:
    """
    Splits documents into chunks whose boundaries align with Flash-Attention
    tile sizes. Aligned boundaries prevent attention from spanning a tile
    edge at a sentence-internal split, reducing cache waste.

    Usage:
        chunker = TileAlignedChunker(tile_size=128, overlap_tokens=16)
        chunks = chunker.chunk(doc_id="paper-1", tokens=token_ids)
        for chunk in chunks:
            assert chunk.token_count % chunker.tile_size == 0 or chunk.chunk_index == len(chunks) - 1
    """

    def __init__(self, tile_size: int = 128, overlap_tokens: int = 0,
                 max_chunk_tiles: int = 16):
        self.tile_size = tile_size
        self.overlap = overlap_tokens
        self.max_tokens = tile_size * max_chunk_tiles

    def chunk(self, doc_id: str, tokens: List[int]) -> List[DocumentChunk]:
        stride = self.max_tokens - self.overlap
        chunks = []
        idx = 0
        chunk_i = 0
        while idx < len(tokens):
            end = min(idx + self.max_tokens, len(tokens))
            raw = tokens[idx:end]
            # Pad to next tile boundary if not the last chunk
            if end < len(tokens):
                remainder = len(raw) % self.tile_size
                if remainder:
                    pad = self.tile_size - remainder
                    raw = raw + [0] * pad
            chunks.append(DocumentChunk(
                doc_id=doc_id,
                chunk_index=chunk_i,
                tokens=raw,
                token_count=len(raw),
                tile_aligned=(len(raw) % self.tile_size == 0),
            ))
            idx += stride
            chunk_i += 1
        return chunks

    def optimal_chunk_size(self, doc_len: int) -> int:
        """Return the largest tile-aligned chunk size ≤ max_tokens."""
        tiles = min(self.max_tokens, doc_len) // self.tile_size
        return tiles * self.tile_size


@dataclass
class PackedBatch:
    chunks: List[DocumentChunk]
    total_tokens: int
    utilisation: float   # fraction of context window used


class ChunkPacker:
    """
    Bin-packs multiple document chunks into a single LLM request.
    Uses first-fit decreasing to maximise context window utilisation.

    Usage:
        packer = ChunkPacker(context_window=8192)
        batches = packer.pack(all_chunks)
        for batch in batches:
            response = await llm.complete(batch.chunks)
    """

    def __init__(self, context_window: int = 8192):
        self._window = context_window

    def pack(self, chunks: List[DocumentChunk]) -> List[PackedBatch]:
        sorted_chunks = sorted(chunks, key=lambda c: c.token_count, reverse=True)
        batches: List[PackedBatch] = []
        for chunk in sorted_chunks:
            placed = False
            for batch in batches:
                if batch.total_tokens + chunk.token_count <= self._window:
                    batch.chunks.append(chunk)
                    batch.total_tokens += chunk.token_count
                    batch.utilisation = batch.total_tokens / self._window
                    placed = True
                    break
            if not placed:
                batches.append(PackedBatch(
                    chunks=[chunk],
                    total_tokens=chunk.token_count,
                    utilisation=chunk.token_count / self._window,
                ))
        return batches
```

---

## Solution 2: SlidingWindowAttentionScheduler — Long-Doc with Local+Global Tokens

For documents exceeding the context window, schedule overlapping windows with global sink tokens so Flash-Attention sees full coverage without recomputation.

```python
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class AttentionWindow:
    window_index: int
    local_tokens: List[int]
    global_tokens: List[int]   # sink tokens present in every window
    position_offset: int       # absolute position of local_tokens[0]
    total_tokens: int


class SlidingWindowAttentionScheduler:
    """
    Schedules Flash-Attention windows for documents longer than context_window.
    Each window contains:
    - global_tokens: BOS + first K tokens (attend globally, like Longformer)
    - local_tokens: a contiguous tile-aligned slice of the document

    Usage:
        scheduler = SlidingWindowAttentionScheduler(
            context_window=8192, global_tokens=64, stride_tiles=60, tile_size=128
        )
        doc_tokens = tokenizer.encode(long_document)
        for window in scheduler.schedule(doc_tokens):
            result = await llm_tile(window.global_tokens + window.local_tokens)
            aggregate(result, offset=window.position_offset)
    """

    def __init__(self, context_window: int = 8192,
                 global_tokens: int = 64,
                 stride_tiles: int = 60,
                 tile_size: int = 128):
        self._window = context_window
        self._global = global_tokens
        self._stride = stride_tiles * tile_size
        self._local_capacity = context_window - global_tokens
        self._tile = tile_size

    def schedule(self, tokens: List[int]) -> List[AttentionWindow]:
        global_toks = tokens[:self._global]
        body = tokens[self._global:]
        windows = []
        idx = 0
        win_i = 0
        while idx < len(body):
            end = min(idx + self._local_capacity, len(body))
            local = body[idx:end]
            # Pad to tile boundary
            remainder = len(local) % self._tile
            if remainder and end < len(body):
                local = local + [0] * (self._tile - remainder)
            windows.append(AttentionWindow(
                window_index=win_i,
                local_tokens=local,
                global_tokens=global_toks,
                position_offset=self._global + idx,
                total_tokens=len(global_toks) + len(local),
            ))
            idx += self._stride
            win_i += 1
        return windows

    def coverage(self, doc_len: int) -> float:
        windows = self.schedule(list(range(doc_len)))
        covered = set()
        for w in windows:
            for i, _ in enumerate(w.local_tokens):
                covered.add(w.position_offset + i)
        return len(covered) / doc_len
```

---

## Solution 3: FlashAttentionAwareRetriever — Pack Retrieved Chunks Optimally

After semantic retrieval, re-order and pack chunks so they tile-align inside the context window with minimum padding waste.

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    token_count: int
    score: float
    metadata: Dict[str, Any]


class FlashAttentionAwareRetriever:
    """
    Post-processes retrieved chunks to maximise Flash-Attention efficiency:
    1. Filters chunks whose combined length exceeds the context budget.
    2. Packs chunks in score-descending order, snapping each to a tile boundary.
    3. Reports padding waste so callers can tune retrieval top-k.

    Usage:
        retriever = FlashAttentionAwareRetriever(
            context_budget=6144, tile_size=128, system_overhead=512
        )
        packed, stats = retriever.pack_for_context(retrieved_chunks)
        context_str = "\\n\\n".join(c.text for c in packed)
    """

    def __init__(self, context_budget: int = 6144,
                 tile_size: int = 128,
                 system_overhead: int = 512):
        self._budget = context_budget - system_overhead
        self._tile = tile_size

    def _snap_to_tile(self, n: int) -> int:
        r = n % self._tile
        return n if r == 0 else n + (self._tile - r)

    def pack_for_context(
        self, chunks: List[RetrievedChunk]
    ) -> Tuple[List[RetrievedChunk], Dict[str, Any]]:
        sorted_chunks = sorted(chunks, key=lambda c: c.score, reverse=True)
        packed = []
        used = 0
        raw_total = 0
        for chunk in sorted_chunks:
            snapped = self._snap_to_tile(chunk.token_count)
            if used + snapped > self._budget:
                continue
            packed.append(chunk)
            used += snapped
            raw_total += chunk.token_count

        padding_tokens = used - raw_total
        stats = {
            "chunks_selected": len(packed),
            "chunks_dropped": len(chunks) - len(packed),
            "tokens_used": used,
            "padding_tokens": padding_tokens,
            "padding_pct": round(padding_tokens / used * 100, 1) if used else 0,
            "budget_utilisation": round(used / self._budget, 3),
        }
        return packed, stats
```

---

## Solution 4: MultiQueryPacker — Batch Independent Queries into One Pass

When an agent issues N independent queries to the same context, pack them into a single forward pass using Flash-Attention's batch dimension.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PackedQuery:
    query_id: str
    system_prompt: str
    user_message: str
    token_estimate: int
    result: Optional[Any] = None


class MultiQueryPacker:
    """
    Collects independent LLM queries and issues them as a batched request,
    exploiting Flash-Attention's O(batch * n) complexity rather than
    O(batch) * O(n) for separate sequential calls.

    Usage:
        packer = MultiQueryPacker(llm_batch_fn=llm.batch_complete,
                                   max_batch=8, max_wait_ms=20)
        asyncio.create_task(packer.run())

        result = await packer.query(system="You are...", user="Summarise X")
        result2 = await packer.query(system="You are...", user="Classify Y")
    """

    def __init__(self, llm_batch_fn: Callable,
                 max_batch: int = 8,
                 max_wait_ms: float = 20.0):
        self._fn = llm_batch_fn
        self._max_batch = max_batch
        self._max_wait = max_wait_ms / 1000.0
        self._queue: asyncio.Queue = asyncio.Queue()

    async def query(self, system: str, user: str,
                    token_estimate: int = 256) -> Any:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((PackedQuery(
            query_id=str(id(fut)),
            system_prompt=system,
            user_message=user,
            token_estimate=token_estimate,
        ), fut))
        return await fut

    async def run(self):
        while True:
            batch = []
            futs = []
            try:
                item, fut = await asyncio.wait_for(
                    self._queue.get(), timeout=self._max_wait
                )
                batch.append(item)
                futs.append(fut)
            except asyncio.TimeoutError:
                continue

            # Drain up to max_batch
            while len(batch) < self._max_batch:
                try:
                    item, fut = self._queue.get_nowait()
                    batch.append(item)
                    futs.append(fut)
                except asyncio.QueueEmpty:
                    break

            try:
                results = await self._fn(batch)
                for fut, result in zip(futs, results):
                    fut.set_result(result)
            except Exception as exc:
                for fut in futs:
                    if not fut.done():
                        fut.set_exception(exc)
```

---

## Solution 5: ContextWindowUtilisationTracker — Measure Packing Efficiency

Track tile utilisation across requests to identify padding waste and auto-tune chunk sizes.

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List


@dataclass
class RequestRecord:
    timestamp: float
    context_window: int
    tokens_used: int
    padding_tokens: int
    num_chunks: int


class ContextWindowUtilisationTracker:
    """
    Tracks Flash-Attention context window utilisation across agent requests.
    Reports average padding waste, utilisation, and suggests optimal chunk sizes.

    Usage:
        tracker = ContextWindowUtilisationTracker(context_window=8192)
        tracker.record(tokens_used=6400, padding_tokens=256, num_chunks=5)
        report = tracker.report()
        if report["avg_utilisation"] < 0.6:
            logging.warning("Context window under-utilised — increase retrieval top-k")
    """

    def __init__(self, context_window: int = 8192,
                 history_size: int = 1000):
        self._window = context_window
        self._records: Deque[RequestRecord] = deque(maxlen=history_size)

    def record(self, tokens_used: int, padding_tokens: int = 0,
               num_chunks: int = 1):
        self._records.append(RequestRecord(
            timestamp=time.time(),
            context_window=self._window,
            tokens_used=tokens_used,
            padding_tokens=padding_tokens,
            num_chunks=num_chunks,
        ))

    def report(self) -> Dict:
        if not self._records:
            return {}
        utilisations = [r.tokens_used / r.context_window for r in self._records]
        padding_pcts = [
            r.padding_tokens / r.tokens_used if r.tokens_used else 0
            for r in self._records
        ]
        return {
            "requests_sampled": len(self._records),
            "avg_utilisation": round(sum(utilisations) / len(utilisations), 3),
            "p50_utilisation": round(sorted(utilisations)[len(utilisations)//2], 3),
            "avg_padding_pct": round(sum(padding_pcts) / len(padding_pcts) * 100, 2),
            "avg_chunks_per_request": round(
                sum(r.num_chunks for r in self._records) / len(self._records), 1
            ),
            "recommendation": self._recommend(sum(utilisations) / len(utilisations)),
        }

    def _recommend(self, avg_util: float) -> str:
        if avg_util < 0.5:
            return "Increase retrieval top-k or pack more documents per request"
        if avg_util > 0.95:
            return "Reduce chunk size or increase retrieval budget filtering"
        return "Utilisation is healthy (50–95%)"
```

---

## Solution 6: FlashAttentionContextManager — End-to-End Agent Integration

Combines chunking, packing, scheduling, and tracking into a single context manager used at the agent request boundary.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, List, Optional


class FlashAttentionContextManager:
    """
    End-to-end Flash-Attention-aware context manager for agents.
    At each turn: chunks retrieved docs, packs into tile-aligned batches,
    tracks utilisation, and issues the minimal number of LLM calls.

    Usage:
        fa_ctx = FlashAttentionContextManager(
            context_window=8192, tile_size=128, llm_fn=llm.complete
        )
        response = await fa_ctx.complete(
            system_prompt="You are a helpful assistant.",
            user_query="Summarise these papers.",
            retrieved_docs=documents,
        )
    """

    def __init__(self, context_window: int = 8192,
                 tile_size: int = 128,
                 system_overhead: int = 512,
                 llm_fn: Optional[Callable] = None):
        self._chunker = TileAlignedChunker(tile_size=tile_size)
        self._retriever_packer = FlashAttentionAwareRetriever(
            context_budget=context_window,
            tile_size=tile_size,
            system_overhead=system_overhead,
        )
        self._tracker = ContextWindowUtilisationTracker(context_window)
        self._llm = llm_fn
        self._tile = tile_size

    async def complete(self, system_prompt: str, user_query: str,
                       retrieved_docs: List[RetrievedChunk]) -> Any:
        packed, stats = self._retriever_packer.pack_for_context(retrieved_docs)
        self._tracker.record(
            tokens_used=stats["tokens_used"],
            padding_tokens=stats["padding_tokens"],
            num_chunks=stats["chunks_selected"],
        )
        context_text = "\n\n".join(
            f"[{c.chunk_id}]\n{c.text}" for c in packed
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Context:\n{context_text}\n\n{user_query}"},
        ]
        if self._llm:
            return await self._llm(messages)
        return {"messages": messages, "packing_stats": stats}

    def utilisation_report(self) -> dict:
        return self._tracker.report()
```

---

## Comparison

| Approach | Tile Alignment | Multi-Doc Packing | Long-Doc Windowing | Batch Queries | Metrics |
|---|---|---|---|---|---|
| **TileAlignedChunker + ChunkPacker** | Yes | Yes | No | No | No |
| **SlidingWindowAttentionScheduler** | Yes | No | Yes | No | No |
| **FlashAttentionAwareRetriever** | Yes (snap) | Yes | No | No | No |
| **MultiQueryPacker** | No | No | No | Yes | No |
| **UtilisationTracker** | N/A | N/A | N/A | N/A | Yes |
| **FlashAttentionContextManager** | Yes | Yes | No | No | Yes |

**Key insight**: Flash-Attention's memory advantage is only realised when requests avoid excessive padding and the agent packs multiple short documents into one context window rather than issuing a separate call per document. Tile-align chunk boundaries and use first-fit-decreasing bin packing to push utilisation above 70%.
