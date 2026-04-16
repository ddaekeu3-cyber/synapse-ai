---
title: "Agent Doesn't Implement Memory-Mapped File Cache for Large Context"
description: "AI agents that repeatedly load large context files, embedding indexes, or knowledge bases from disk pay full I/O cost on every request. Memory-mapped files let the OS page these objects on demand with zero-copy reads, cutting cold-start latency by orders of magnitude."
date: 2025-01-31
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-memory-mapped-file-cache-for-large-context
tags:
  - mmap
  - performance
  - caching
  - context-window
  - large-files
  - zero-copy
  - knowledge-base
symptoms:
  - "Agent cold-start takes 10–30 seconds loading large embedding indexes"
  - "Reading the same knowledge-base file on every request causes noticeable latency spikes"
  - "Memory usage spikes because large files are fully loaded into Python heap"
  - "Multiple agent workers each maintain a full in-memory copy of shared data"
  - "Disk I/O metrics show the same file read repeatedly across agent instances"
---

## Problem

Agents backed by large knowledge bases, vector indexes, or multi-megabyte system prompts suffer repeated disk-read overhead. A naive `open(...).read()` loads the entire file into process memory on every access. Even with a Python-level dict cache, the first read per process is slow and each worker process holds its own heap copy — wasting RAM proportional to worker count.

Memory-mapped files solve both problems:

- **Zero-copy reads**: the OS maps file pages directly into virtual address space; data is only faulted in as pages are accessed.
- **Shared physical memory**: all worker processes mapping the same file share the same physical pages, so 10 workers don't need 10× the RAM.
- **OS-managed cache**: the kernel keeps hot pages in the page cache and evicts cold pages automatically under memory pressure.

---

## Solution 1: Read-Only MMap Context Loader

Map a large context file (system prompt, knowledge base) read-only. Return slices without copying.

```python
import mmap
import os
import struct
from pathlib import Path
from typing import Optional


class MMapContextLoader:
    """
    Maps a large text or binary file read-only.
    Returns string slices without heap allocation.

    Usage:
        loader = MMapContextLoader("/data/knowledge_base.txt")
        loader.open()
        chunk = loader.read(offset=0, length=4096)
        loader.close()

        # Or as context manager:
        with MMapContextLoader("/data/knowledge_base.txt") as loader:
            text = loader.read_all()
    """

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._file = None
        self._mm: Optional[mmap.mmap] = None

    def open(self):
        self._file = open(self._path, "rb")
        self._mm = mmap.mmap(
            self._file.fileno(),
            length=0,                  # 0 = map entire file
            access=mmap.ACCESS_READ,
        )

    def close(self):
        if self._mm:
            self._mm.close()
            self._mm = None
        if self._file:
            self._file.close()
            self._file = None

    def read(self, offset: int = 0, length: Optional[int] = None) -> bytes:
        if self._mm is None:
            raise RuntimeError("MMapContextLoader not opened")
        end = (offset + length) if length else len(self._mm)
        return self._mm[offset:end]

    def read_all(self) -> str:
        return self.read().decode("utf-8", errors="replace")

    def size(self) -> int:
        return len(self._mm) if self._mm else 0

    def find(self, pattern: bytes, start: int = 0) -> int:
        """O(n) substring search without copying."""
        return self._mm.find(pattern, start)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
```

---

## Solution 2: MMap Numpy Array Index (Embedding Cache)

Store a float32 embedding matrix on disk in raw binary format. Map it with numpy's `memmap` so individual row lookups fault in only the needed pages rather than loading the entire matrix.

```python
import numpy as np
from pathlib import Path
from typing import List, Optional, Tuple


class MMapEmbeddingIndex:
    """
    Persistent embedding matrix backed by a memory-mapped numpy array.
    Supports O(1) row lookup with only the accessed pages loaded into RAM.

    Usage:
        # Build once:
        index = MMapEmbeddingIndex.create("/data/embeddings.bin", dim=1536, n=500000)
        index.add(0, embedding_vector)
        index.flush()

        # Query (zero-copy):
        idx = MMapEmbeddingIndex.load("/data/embeddings.bin")
        row = idx.get(42)
        scores = idx.cosine_top_k(query_vec, k=10)
    """

    HEADER_MAGIC = b"MMAP_EMB\x00"
    HEADER_SIZE = 128   # bytes reserved for header

    def __init__(self, path: str, mode: str, dim: int, n: int):
        self._path = path
        self._dim = dim
        self._n = n
        self._mm: np.memmap = np.memmap(
            path,
            dtype="float32",
            mode=mode,
            offset=self.HEADER_SIZE,
            shape=(n, dim),
        )

    @classmethod
    def create(cls, path: str, dim: int, n: int) -> "MMapEmbeddingIndex":
        # Write header
        with open(path, "wb") as f:
            header = cls.HEADER_MAGIC
            header += dim.to_bytes(4, "little")
            header += n.to_bytes(8, "little")
            header += b"\x00" * (cls.HEADER_SIZE - len(header))
            f.write(header)
            # Allocate space for the matrix
            f.seek(cls.HEADER_SIZE + n * dim * 4 - 1)
            f.write(b"\x00")
        return cls(path, mode="r+", dim=dim, n=n)

    @classmethod
    def load(cls, path: str) -> "MMapEmbeddingIndex":
        with open(path, "rb") as f:
            header = f.read(cls.HEADER_SIZE)
        assert header[:len(cls.HEADER_MAGIC)] == cls.HEADER_MAGIC
        dim = int.from_bytes(header[len(cls.HEADER_MAGIC):len(cls.HEADER_MAGIC)+4], "little")
        n   = int.from_bytes(header[len(cls.HEADER_MAGIC)+4:len(cls.HEADER_MAGIC)+12], "little")
        return cls(path, mode="r", dim=dim, n=n)

    def add(self, idx: int, vector: np.ndarray):
        self._mm[idx] = vector.astype("float32")

    def get(self, idx: int) -> np.ndarray:
        return self._mm[idx]   # page-faulted on demand

    def flush(self):
        self._mm.flush()

    def cosine_top_k(self, query: np.ndarray, k: int = 10) -> List[Tuple[int, float]]:
        q = query.astype("float32")
        q_norm = q / (np.linalg.norm(q) + 1e-9)
        # Only load needed pages via batched matmul
        batch = 10_000
        scores = np.empty(self._n, dtype="float32")
        for start in range(0, self._n, batch):
            end = min(start + batch, self._n)
            chunk = self._mm[start:end]
            norms = np.linalg.norm(chunk, axis=1, keepdims=True) + 1e-9
            scores[start:end] = (chunk / norms) @ q_norm
        top_idx = np.argpartition(scores, -k)[-k:]
        top_idx = top_idx[np.argsort(scores[top_idx])[::-1]]
        return [(int(i), float(scores[i])) for i in top_idx]
```

---

## Solution 3: Shared-Memory Context Store (Multi-Process)

Use Python's `multiprocessing.shared_memory` to map a context blob into all worker processes simultaneously. Workers read without IPC overhead; one writer updates the shared segment atomically.

```python
import struct
import time
from multiprocessing import shared_memory
from typing import Optional


_HEADER_FMT = "!QQ"   # (version: u64, data_len: u64)
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_MAX_DATA = 64 * 1024 * 1024   # 64 MB


class SharedContextStore:
    """
    Stores the current agent system prompt / context blob in shared memory
    accessible by all worker processes on the same host.

    Writer (main process):
        store = SharedContextStore.create("agent_ctx")
        store.write(context_bytes)

    Reader (each worker):
        store = SharedContextStore.attach("agent_ctx")
        ctx = store.read()
    """

    def __init__(self, shm: shared_memory.SharedMemory):
        self._shm = shm
        self._buf = shm.buf

    @classmethod
    def create(cls, name: str) -> "SharedContextStore":
        try:
            shm = shared_memory.SharedMemory(
                name=name, create=True,
                size=_HEADER_SIZE + _MAX_DATA,
            )
        except FileExistsError:
            shm = shared_memory.SharedMemory(name=name, create=False)
        store = cls(shm)
        store._write_header(version=0, data_len=0)
        return store

    @classmethod
    def attach(cls, name: str) -> "SharedContextStore":
        shm = shared_memory.SharedMemory(name=name, create=False)
        return cls(shm)

    def write(self, data: bytes):
        if len(data) > _MAX_DATA:
            raise ValueError(f"Context too large: {len(data)} > {_MAX_DATA}")
        version, _ = self._read_header()
        # Write data first, then bump version (readers see consistent state)
        self._buf[_HEADER_SIZE: _HEADER_SIZE + len(data)] = data
        self._write_header(version + 1, len(data))

    def read(self) -> bytes:
        _, data_len = self._read_header()
        return bytes(self._buf[_HEADER_SIZE: _HEADER_SIZE + data_len])

    def version(self) -> int:
        v, _ = self._read_header()
        return v

    def _write_header(self, version: int, data_len: int):
        self._buf[:_HEADER_SIZE] = struct.pack(_HEADER_FMT, version, data_len)

    def _read_header(self):
        return struct.unpack(_HEADER_FMT, bytes(self._buf[:_HEADER_SIZE]))

    def close(self):
        self._shm.close()

    def unlink(self):
        self._shm.unlink()
```

---

## Solution 4: MMap JSON Knowledge-Base with Lazy Parsing

Map a large JSON knowledge base file but parse only the entries the agent actually needs, avoiding the cost of `json.loads` on the whole file at startup.

```python
import json
import mmap
import re
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple


class LazyMMapJSONIndex:
    """
    Maps a newline-delimited JSON file (one object per line) and parses
    entries on demand.  Builds a byte-offset index at open time (fast scan),
    then deserialises only the lines that are actually requested.

    Usage:
        kb = LazyMMapJSONIndex("/data/knowledge_base.jsonl", key_field="id")
        kb.open()
        entry = kb.get("article_42")   # parses only that line
        for entry in kb.scan(lambda e: e["category"] == "faq"):
            process(entry)
    """

    def __init__(self, path: str | Path, key_field: str = "id"):
        self._path = str(path)
        self._key_field = key_field
        self._file = None
        self._mm: Optional[mmap.mmap] = None
        self._offsets: Dict[str, Tuple[int, int]] = {}  # key -> (start, end)

    def open(self):
        self._file = open(self._path, "rb")
        self._mm = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        self._build_index()

    def _build_index(self):
        """Fast O(n) byte scan to record line offsets without full JSON parse."""
        pos = 0
        size = len(self._mm)
        key_pat = re.compile(
            ('"' + self._key_field + r'"\s*:\s*"([^"]+)"').encode()
        )
        while pos < size:
            line_end = self._mm.find(b"\n", pos)
            if line_end == -1:
                line_end = size
            line = self._mm[pos:line_end]
            m = key_pat.search(line)
            if m:
                key = m.group(1).decode("utf-8", errors="replace")
                self._offsets[key] = (pos, line_end)
            pos = line_end + 1

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        offsets = self._offsets.get(key)
        if offsets is None:
            return None
        start, end = offsets
        return json.loads(self._mm[start:end])

    def scan(self, predicate=None) -> Iterator[Dict[str, Any]]:
        for key, (start, end) in self._offsets.items():
            entry = json.loads(self._mm[start:end])
            if predicate is None or predicate(entry):
                yield entry

    def close(self):
        if self._mm:
            self._mm.close()
        if self._file:
            self._file.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def __len__(self):
        return len(self._offsets)
```

---

## Solution 5: Write-Through MMap Cache for LLM Prompt Templates

Cache rendered prompt templates on a shared memory-mapped file. Subsequent renders with the same template/variable hash return the cached bytes without re-rendering.

```python
import hashlib
import json
import mmap
import struct
import time
from pathlib import Path
from typing import Dict, Optional


_ENTRY_HEADER = "!16sIQ"   # (md5: 16 bytes, data_len: u32, timestamp: u64)
_ENTRY_HEADER_SIZE = struct.calcsize(_ENTRY_HEADER)


class MMapPromptCache:
    """
    Write-through cache for rendered prompt strings backed by a mmap'd file.
    Multiple processes sharing the same file share cached prompts.

    Usage:
        cache = MMapPromptCache("/tmp/prompt_cache.bin", capacity=1000)
        cache.open()

        key = cache.put(template_name, variables, rendered_text)
        hit = cache.get(template_name, variables)
    """

    def __init__(self, path: str, capacity: int = 500,
                 max_entry_bytes: int = 32_768):
        self._path = path
        self._capacity = capacity
        self._max_entry = max_entry_bytes
        self._entries: Dict[str, int] = {}   # hex_hash -> file offset
        self._next_offset = 0
        self._mm: Optional[mmap.mmap] = None
        self._file = None
        self._total_size = capacity * max_entry_bytes

    def open(self):
        p = Path(self._path)
        if not p.exists():
            p.write_bytes(b"\x00" * self._total_size)
        self._file = open(self._path, "r+b")
        self._mm = mmap.mmap(self._file.fileno(), self._total_size)

    def _hash(self, template: str, variables: dict) -> str:
        payload = json.dumps({"t": template, "v": variables}, sort_keys=True)
        return hashlib.md5(payload.encode()).hexdigest()

    def put(self, template: str, variables: dict, rendered: str) -> str:
        h = self._hash(template, variables)
        data = rendered.encode("utf-8")
        if len(data) > self._max_entry - _ENTRY_HEADER_SIZE:
            return h  # too large to cache
        if h in self._entries:
            return h  # already cached
        if self._next_offset + _ENTRY_HEADER_SIZE + len(data) > self._total_size:
            return h  # cache full

        offset = self._next_offset
        header = struct.pack(
            _ENTRY_HEADER,
            bytes.fromhex(h[:32]),
            len(data),
            int(time.time()),
        )
        self._mm[offset: offset + _ENTRY_HEADER_SIZE] = header
        self._mm[offset + _ENTRY_HEADER_SIZE: offset + _ENTRY_HEADER_SIZE + len(data)] = data
        self._entries[h] = offset
        self._next_offset += _ENTRY_HEADER_SIZE + len(data)
        return h

    def get(self, template: str, variables: dict) -> Optional[str]:
        h = self._hash(template, variables)
        offset = self._entries.get(h)
        if offset is None:
            return None
        _, data_len, _ = struct.unpack_from(_ENTRY_HEADER, self._mm, offset)
        start = offset + _ENTRY_HEADER_SIZE
        return self._mm[start: start + data_len].tobytes().decode("utf-8")

    def close(self):
        if self._mm:
            self._mm.flush()
            self._mm.close()
        if self._file:
            self._file.close()
```

---

## Solution 6: MMap-Backed Agent Context Manager

Drop-in manager that transparently loads large context files via mmap and exposes a simple string interface. Falls back to a standard file read if the file is small enough to fit entirely in a Python string.

```python
import mmap
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


_MMAP_THRESHOLD_BYTES = 512 * 1024   # 512 KB


@dataclass
class ContextHandle:
    path: str
    size: int
    is_mmap: bool
    _data: Optional[str] = None       # for small files
    _mm: Optional[mmap.mmap] = None   # for large files
    _file: Optional[object] = None

    def read(self, offset: int = 0, length: Optional[int] = None) -> str:
        if self.is_mmap:
            end = (offset + length) if length else self.size
            return self._mm[offset:end].decode("utf-8", errors="replace")
        data = self._data or ""
        return data[offset: offset + length] if length else data[offset:]

    def close(self):
        if self._mm:
            self._mm.close()
        if self._file:
            self._file.close()


class AgentContextManager:
    """
    Manages large context files for agent sessions.
    Files above the threshold are memory-mapped; smaller ones are heap-loaded.

    Usage:
        mgr = AgentContextManager()
        ctx = mgr.load("/data/system_prompt.txt")
        agent.set_context(ctx.read())
        mgr.release("/data/system_prompt.txt")
    """

    def __init__(self, threshold: int = _MMAP_THRESHOLD_BYTES):
        self._threshold = threshold
        self._handles: Dict[str, ContextHandle] = {}

    def load(self, path: str | Path) -> ContextHandle:
        path = str(path)
        if path in self._handles:
            return self._handles[path]

        size = os.path.getsize(path)
        if size >= self._threshold:
            f = open(path, "rb")
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            handle = ContextHandle(
                path=path, size=size, is_mmap=True,
                _mm=mm, _file=f,
            )
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
            handle = ContextHandle(
                path=path, size=size, is_mmap=False, _data=data,
            )

        self._handles[path] = handle
        return handle

    def release(self, path: str):
        handle = self._handles.pop(str(path), None)
        if handle:
            handle.close()

    def release_all(self):
        for h in list(self._handles.values()):
            h.close()
        self._handles.clear()

    def stats(self) -> dict:
        return {
            "loaded_files": len(self._handles),
            "mmap_files": sum(1 for h in self._handles.values() if h.is_mmap),
            "total_bytes": sum(h.size for h in self._handles.values()),
        }
```

---

## Comparison

| Approach | Access Pattern | RAM per Worker | Write Support |
|---|---|---|---|
| **Read-Only MMap Loader** | Sequential / random byte slices | OS page-cache (shared) | No |
| **MMap Numpy Embedding Index** | Row-by-row vector lookup | Pages faulted in on access | Yes (r+ mode) |
| **Shared Memory Context Store** | Cross-process single blob | Single physical copy | Yes (atomic) |
| **Lazy MMap JSON Index** | Key-based entry lookup | Header only at open | No |
| **Write-Through Prompt Cache** | Hash-keyed rendered prompts | Cache file size | Yes (append) |
| **Agent Context Manager** | Transparent load/read | Small: heap; Large: mmap | No |

**Recommendation**: use `MMapEmbeddingIndex` for vector stores, `SharedContextStore` for cross-worker prompt sharing, and `LazyMMapJSONIndex` for knowledge bases over 50 MB. All approaches share the same physical pages across OS processes — RAM usage stays flat as worker count grows.
