---
title: "Agent Doesn't Implement Adaptive Payload Compression"
description: "Agents that send large tool results, embeddings, and context payloads without compression waste bandwidth, inflate latency, and hit API body size limits. Implement adaptive payload compression that selects algorithm and level based on content type, payload size, and observed compression ratios at runtime."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-adaptive-payload-compression
tags: [compression, payload-optimization, performance, bandwidth, gzip, zstd, latency]
symptoms:
  - "Tool results with large JSON arrays transferred uncompressed over 100KB"
  - "Embedding store responses take 400ms due to raw float array transfer"
  - "API gateway rejects payloads > 6MB that compression would reduce to < 1MB"
  - "Agent applies gzip to already-compressed PNG/JPEG tool outputs — wasting CPU"
  - "Same compression algorithm used for 50-byte and 500KB payloads"
---

## Why This Happens

Compression is not universally beneficial: small payloads (< 1KB) gain nothing from compression overhead, already-compressed formats (JPEG, PNG, gzip) expand under re-compression, and high-entropy data (random bytes, encrypted blobs) compresses poorly. Adaptive compression profiles content type, measures payload size, and selects the algorithm and level that maximizes the ratio of bandwidth savings to CPU cost — bypassing compression entirely when it would hurt.

## Solution 1: Content-Aware Compression Selector

```python
import gzip
import io
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class CompressionAlgorithm(str, Enum):
    NONE = "none"
    GZIP = "gzip"
    ZLIB = "zlib"
    ZSTD = "zstd"     # requires python-zstandard

class ContentType(str, Enum):
    JSON = "json"
    TEXT = "text"
    BINARY = "binary"
    EMBEDDING = "embedding"   # float arrays
    ALREADY_COMPRESSED = "already_compressed"

@dataclass
class CompressionPlan:
    algorithm: CompressionAlgorithm
    level: int        # 1 (fast) to 9 (best ratio)
    reason: str

# Signatures for already-compressed formats — skip recompression
COMPRESSED_MAGIC = [
    b'\xff\xd8\xff',        # JPEG
    b'\x89PNG',             # PNG
    b'GIF8',                # GIF
    b'PK\x03\x04',         # ZIP
    b'\x1f\x8b',           # GZIP
    b'\x28\xb5\x2f\xfd',  # ZSTD
]

class ContentAwareCompressionSelector:
    """
    Selects compression algorithm and level based on content type,
    payload size, and entropy estimate. Skips compression entirely
    for already-compressed or tiny payloads.
    """

    SKIP_BELOW_BYTES = 512
    FAST_BELOW_BYTES = 4096
    HIGH_RATIO_ABOVE_BYTES = 65536

    def detect_content_type(self, data: bytes) -> ContentType:
        for magic in COMPRESSED_MAGIC:
            if data[:len(magic)] == magic:
                return ContentType.ALREADY_COMPRESSED
        # Heuristic: valid UTF-8 and starts with { or [ → JSON/text
        try:
            text = data[:256].decode("utf-8")
            if text.lstrip().startswith(("{", "[")):
                return ContentType.JSON
            return ContentType.TEXT
        except UnicodeDecodeError:
            return ContentType.BINARY

    def estimate_entropy(self, data: bytes, sample: int = 1024) -> float:
        """Shannon entropy estimate on a sample. High entropy → poor compression."""
        import math
        sample_data = data[:sample]
        if not sample_data:
            return 0.0
        freq = {}
        for b in sample_data:
            freq[b] = freq.get(b, 0) + 1
        n = len(sample_data)
        return -sum((c / n) * math.log2(c / n) for c in freq.values())

    def select(self, data: bytes, content_type_hint: Optional[ContentType] = None) -> CompressionPlan:
        size = len(data)
        ct = content_type_hint or self.detect_content_type(data)

        if ct == ContentType.ALREADY_COMPRESSED:
            return CompressionPlan(CompressionAlgorithm.NONE, 0, "already_compressed")

        if size < self.SKIP_BELOW_BYTES:
            return CompressionPlan(CompressionAlgorithm.NONE, 0, "too_small")

        entropy = self.estimate_entropy(data)
        if entropy > 7.5:   # near-random — compression won't help
            return CompressionPlan(CompressionAlgorithm.NONE, 0, "high_entropy")

        if size < self.FAST_BELOW_BYTES:
            # Small: use fast gzip
            return CompressionPlan(CompressionAlgorithm.GZIP, 1, "small_fast")

        if ct in (ContentType.JSON, ContentType.TEXT):
            if size > self.HIGH_RATIO_ABOVE_BYTES:
                # Large text: use zstd level 3 (best ratio/speed balance)
                return CompressionPlan(CompressionAlgorithm.ZSTD, 3, "large_text_zstd")
            return CompressionPlan(CompressionAlgorithm.GZIP, 6, "medium_text_gzip")

        if ct == ContentType.EMBEDDING:
            # Float arrays compress well with zlib level 1 (fast, predictable)
            return CompressionPlan(CompressionAlgorithm.ZLIB, 1, "embedding_zlib_fast")

        return CompressionPlan(CompressionAlgorithm.GZIP, 3, "default_gzip")
```

## Solution 2: Adaptive Compressor with Ratio Feedback

```python
import gzip
import io
import time
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

@dataclass
class CompressionResult:
    compressed_data: bytes
    algorithm: str
    original_size: int
    compressed_size: int
    compression_ms: float

    @property
    def ratio(self) -> float:
        return self.compressed_size / max(self.original_size, 1)

    @property
    def savings_bytes(self) -> int:
        return self.original_size - self.compressed_size

class AdaptiveCompressor:
    """
    Compresses payloads using selected algorithm.
    Tracks observed compression ratios per content type to
    dynamically skip compression when historical ratio shows < 10% savings.
    """

    def __init__(self, selector: ContentAwareCompressionSelector):
        self._selector = selector
        # content_type -> deque of (original_size, compressed_size)
        self._ratio_history: Dict[str, Deque[Tuple[int, int]]] = {}

    def _get_ratio_history(self, key: str) -> Deque[Tuple[int, int]]:
        if key not in self._ratio_history:
            self._ratio_history[key] = deque(maxlen=50)
        return self._ratio_history[key]

    def _historical_ratio(self, key: str) -> Optional[float]:
        history = self._ratio_history.get(key, deque())
        if len(history) < 5:
            return None
        total_orig = sum(o for o, _ in history)
        total_comp = sum(c for _, c in history)
        return total_comp / max(total_orig, 1)

    def compress(self, data: bytes, content_type_hint=None) -> CompressionResult:
        ct = self._selector.detect_content_type(data) if content_type_hint is None else content_type_hint
        plan = self._selector.select(data, ct)

        # Check historical ratio — skip if compression rarely helps
        hist = self._historical_ratio(ct.value if hasattr(ct, 'value') else str(ct))
        if hist is not None and hist > 0.92:
            plan = CompressionPlan(CompressionAlgorithm.NONE, 0, "poor_historical_ratio")

        t0 = time.monotonic()

        if plan.algorithm == CompressionAlgorithm.NONE:
            compressed = data
        elif plan.algorithm == CompressionAlgorithm.GZIP:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=plan.level) as f:
                f.write(data)
            compressed = buf.getvalue()
        elif plan.algorithm == CompressionAlgorithm.ZLIB:
            compressed = zlib.compress(data, level=plan.level)
        elif plan.algorithm == CompressionAlgorithm.ZSTD:
            try:
                import zstandard as zstd
                cctx = zstd.ZstdCompressor(level=plan.level)
                compressed = cctx.compress(data)
            except ImportError:
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=plan.level) as f:
                    f.write(data)
                compressed = buf.getvalue()
                plan.algorithm = CompressionAlgorithm.GZIP
        else:
            compressed = data

        elapsed_ms = (time.monotonic() - t0) * 1000

        # Update ratio history
        ct_key = ct.value if hasattr(ct, 'value') else str(ct)
        self._get_ratio_history(ct_key).append((len(data), len(compressed)))

        return CompressionResult(
            compressed_data=compressed,
            algorithm=plan.algorithm.value,
            original_size=len(data),
            compressed_size=len(compressed),
            compression_ms=round(elapsed_ms, 2),
        )

    def decompress(self, data: bytes, algorithm: str) -> bytes:
        if algorithm == "none":
            return data
        if algorithm == "gzip":
            return gzip.decompress(data)
        if algorithm == "zlib":
            return zlib.decompress(data)
        if algorithm == "zstd":
            import zstandard as zstd
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(data)
        raise ValueError(f"Unknown compression algorithm: {algorithm}")
```

## Solution 3: Streaming Compression Pipeline

```python
import asyncio
import gzip
import io
from typing import AsyncIterator

class StreamingCompressor:
    """
    Compresses streaming data incrementally without buffering the entire payload.
    Suitable for large tool results or LLM streaming responses passed through
    a compression layer before hitting the wire.
    """

    def __init__(self, algorithm: str = "gzip", level: int = 3):
        self._algorithm = algorithm
        self._level = level

    async def compress_stream(
        self, chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        """Yields compressed chunks from an async byte stream."""
        if self._algorithm == "gzip":
            async for chunk in self._gzip_stream(chunks):
                yield chunk
        else:
            async for chunk in chunks:
                yield chunk   # fallback: pass through

    async def _gzip_stream(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        buf = io.BytesIO()
        compressor = gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=self._level)
        async for chunk in chunks:
            compressor.write(chunk)
            compressor.flush()
            compressed = buf.getvalue()
            if compressed:
                buf.truncate(0)
                buf.seek(0)
                yield compressed
        compressor.close()
        final = buf.getvalue()
        if final:
            yield final

    async def decompress_stream(
        self, chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        """Decompresses a gzip-compressed byte stream incrementally."""
        decompressor = gzip.GzipFile(fileobj=io.BytesIO(), mode='rb')
        buf = io.BytesIO()
        async for chunk in chunks:
            buf.write(chunk)
        buf.seek(0)
        with gzip.GzipFile(fileobj=buf, mode='rb') as f:
            while True:
                block = f.read(65536)
                if not block:
                    break
                yield block
```

## Solution 4: Compression Cache for Repeated Payloads

```python
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

@dataclass
class CachedCompression:
    compressed_data: bytes
    algorithm: str
    original_size: int
    cached_at: float
    hit_count: int = 0

class CompressionCache:
    """
    LRU cache for compressed payloads. When the same tool result is returned
    repeatedly (e.g., cached DB query results), avoids re-running compression.
    Cache key is SHA-256 of the original payload.
    """

    def __init__(self, max_entries: int = 256, max_entry_bytes: int = 1_048_576):
        self._cache: OrderedDict[str, CachedCompression] = OrderedDict()
        self._max_entries = max_entries
        self._max_entry_bytes = max_entry_bytes
        self._hits = 0
        self._misses = 0

    def _key(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:32]

    def get(self, data: bytes) -> Optional[CachedCompression]:
        key = self._key(data)
        if key not in self._cache:
            self._misses += 1
            return None
        entry = self._cache.pop(key)
        entry.hit_count += 1
        self._cache[key] = entry   # move to end (MRU)
        self._hits += 1
        return entry

    def put(self, data: bytes, compressed: bytes, algorithm: str) -> None:
        if len(compressed) > self._max_entry_bytes:
            return   # skip caching oversized entries
        key = self._key(data)
        if key in self._cache:
            self._cache.pop(key)
        elif len(self._cache) >= self._max_entries:
            self._cache.popitem(last=False)   # evict LRU
        self._cache[key] = CachedCompression(
            compressed_data=compressed,
            algorithm=algorithm,
            original_size=len(data),
            cached_at=time.time(),
        )

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total else 0.0

    def stats(self) -> dict:
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 3),
            "total_compressed_bytes": sum(
                e.compressed_data.__len__() for e in self._cache.values()
            ),
        }
```

## Solution 5: Payload Size Budget Enforcer

```python
import json
from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class SizeBudgetResult:
    within_budget: bool
    original_size: int
    final_size: int
    strategy: str   # "passthrough" | "compressed" | "truncated" | "summarized"
    data: bytes

class PayloadSizeBudgetEnforcer:
    """
    Ensures payloads stay within a configured byte budget.
    First tries compression; if still too large, truncates or summarizes
    the content to fit within the limit.
    """

    def __init__(
        self,
        compressor: AdaptiveCompressor,
        hard_limit_bytes: int = 5_242_880,   # 5MB
        warn_limit_bytes: int = 1_048_576,   # 1MB
    ):
        self._compressor = compressor
        self._hard_limit = hard_limit_bytes
        self._warn_limit = warn_limit_bytes

    def enforce(self, data: bytes, content_hint=None) -> SizeBudgetResult:
        original_size = len(data)

        if original_size <= self._warn_limit:
            return SizeBudgetResult(
                within_budget=True,
                original_size=original_size,
                final_size=original_size,
                strategy="passthrough",
                data=data,
            )

        # Try compression
        result = self._compressor.compress(data, content_hint)
        if result.compressed_size <= self._hard_limit:
            return SizeBudgetResult(
                within_budget=True,
                original_size=original_size,
                final_size=result.compressed_size,
                strategy="compressed",
                data=result.compressed_data,
            )

        # Truncate to hard limit (for binary/opaque data)
        truncated = result.compressed_data[:self._hard_limit]
        return SizeBudgetResult(
            within_budget=False,
            original_size=original_size,
            final_size=len(truncated),
            strategy="truncated",
            data=truncated,
        )

    def enforce_json(self, obj: Any, max_items: int = 1000) -> SizeBudgetResult:
        """Prune large JSON arrays/objects before compression."""
        if isinstance(obj, list) and len(obj) > max_items:
            obj = obj[:max_items]
        elif isinstance(obj, dict):
            # Truncate large string values
            obj = {
                k: (v[:2000] if isinstance(v, str) and len(v) > 2000 else v)
                for k, v in obj.items()
            }
        data = json.dumps(obj).encode("utf-8")
        return self.enforce(data)
```

## Solution 6: Compression Metrics Tracker

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict

@dataclass
class CompressionStats:
    total_original_bytes: int = 0
    total_compressed_bytes: int = 0
    total_operations: int = 0
    total_compression_ms: float = 0.0
    skipped_operations: int = 0

class CompressionMetricsTracker:
    def __init__(self):
        self._by_algorithm: Dict[str, CompressionStats] = defaultdict(CompressionStats)
        self._by_content_type: Dict[str, CompressionStats] = defaultdict(CompressionStats)

    def record(self, result: CompressionResult, content_type: str = "unknown") -> None:
        for stats in [
            self._by_algorithm[result.algorithm],
            self._by_content_type[content_type],
        ]:
            stats.total_original_bytes += result.original_size
            stats.total_compressed_bytes += result.compressed_size
            stats.total_operations += 1
            stats.total_compression_ms += result.compression_ms
            if result.algorithm == "none":
                stats.skipped_operations += 1

    def summary(self) -> dict:
        def stats_dict(s: CompressionStats) -> dict:
            ratio = s.total_compressed_bytes / max(s.total_original_bytes, 1)
            return {
                "operations": s.total_operations,
                "original_mb": round(s.total_original_bytes / 1e6, 3),
                "compressed_mb": round(s.total_compressed_bytes / 1e6, 3),
                "avg_ratio": round(ratio, 3),
                "savings_mb": round((s.total_original_bytes - s.total_compressed_bytes) / 1e6, 3),
                "avg_compression_ms": round(s.total_compression_ms / max(s.total_operations, 1), 2),
                "skipped": s.skipped_operations,
            }
        return {
            "by_algorithm": {k: stats_dict(v) for k, v in self._by_algorithm.items()},
            "by_content_type": {k: stats_dict(v) for k, v in self._by_content_type.items()},
            "generated_at": time.time(),
        }
```

## Comparison

| Approach | Algorithm Selection | Streaming | Caching | Size Enforcement |
|---|---|---|---|---|
| ContentAwareCompressionSelector | Yes (content + size) | No | No | No |
| AdaptiveCompressor | Yes + ratio feedback | No | No | No |
| StreamingCompressor | Fixed algorithm | Yes | No | No |
| CompressionCache | Via compressor | No | Yes (LRU) | No |
| PayloadSizeBudgetEnforcer | Via compressor | No | No | Yes (hard limit) |
| CompressionMetricsTracker | N/A (metrics) | N/A | N/A | N/A |

**Best for production**: Use `ContentAwareCompressionSelector` to pick algorithm per payload, feed through `AdaptiveCompressor` which adjusts based on observed ratios. Add `CompressionCache` to skip re-compression of repeated tool outputs. Enforce hard limits with `PayloadSizeBudgetEnforcer` before sending to APIs with size caps. Track everything with `CompressionMetricsTracker` to verify bandwidth savings justify CPU cost — target > 40% average size reduction for text payloads.
