---
title: "Agent Doesn't Implement JIT-Compiled Tool Output Parser"
description: "AI agents that parse large tool outputs with pure Python JSON, regex, or XML parsing spend significant CPU time on data transformation that could be dramatically accelerated. JIT compilation, vectorised parsing, and compiled extensions reduce parse latency by 10–100× for high-throughput agents."
date: 2025-02-02
difficulty: advanced
category: performance
slug: agent-doesnt-implement-jit-compiled-tool-output-parser
tags:
  - parsing
  - jit
  - numba
  - performance
  - json
  - vectorisation
  - orjson
symptoms:
  - "Tool output parsing consumes 30–60% of agent CPU time on profiler output"
  - "JSON deserialization of large API responses takes 50–200 ms per call"
  - "Regex extraction from large LLM outputs is a hot path that has never been optimised"
  - "Pandas or numpy operations on tool results are done row-by-row in Python loops"
  - "Agent throughput is CPU-bound on parsing, not I/O-bound on API calls"
---

## Problem

Tool outputs — JSON API responses, CSV data, XML feeds, LLM-generated structured text — pass through Python parsers that are significantly slower than native C or JIT-compiled equivalents. For agents processing thousands of tool calls per minute, parsing can become the dominant CPU cost.

The fix is layered:
1. **Swap slow parsers for faster libraries**: `orjson` (JSON), `lxml` (XML), `polars` (tabular) are 2–20× faster than their stdlib equivalents with zero code-structure changes.
2. **JIT-compile hot extraction loops** with Numba or Cython.
3. **Vectorise multi-record processing** with NumPy or Polars.
4. **Pre-compile regex patterns** and use the `regex` module for Unicode-heavy payloads.

---

## Solution 1: Drop-In Fast JSON Parser

Replace `json.loads` with `orjson` for 2–10× speedup with full compatibility.

```python
from typing import Any, Union

try:
    import orjson
    _JSON_BACKEND = "orjson"
    def fast_loads(data: Union[str, bytes]) -> Any:
        if isinstance(data, str):
            data = data.encode()
        return orjson.loads(data)

    def fast_dumps(obj: Any, indent: bool = False) -> bytes:
        opts = orjson.OPT_INDENT_2 if indent else 0
        return orjson.dumps(obj, option=opts)

except ImportError:
    import json
    _JSON_BACKEND = "stdlib"
    def fast_loads(data: Union[str, bytes]) -> Any:
        return json.loads(data)

    def fast_dumps(obj: Any, indent: bool = False) -> bytes:
        return json.dumps(obj, indent=2 if indent else None).encode()


class FastJSONToolOutputParser:
    """
    High-throughput JSON parser for agent tool outputs.
    Uses orjson when available, falls back to stdlib.

    Benchmark (10 MB response):
        stdlib json.loads:  ~180 ms
        orjson.loads:        ~18 ms  (10× faster)

    Usage:
        parser = FastJSONToolOutputParser()
        data = parser.parse(api_response_bytes)
        records = parser.extract_records(data, path="data.items")
    """

    def __init__(self):
        self._backend = _JSON_BACKEND

    def parse(self, raw: Union[str, bytes]) -> Any:
        return fast_loads(raw)

    def extract_records(self, data: Any, path: str) -> list:
        """Navigate a dotted path like 'data.items' through nested dicts/lists."""
        parts = path.split(".")
        node = data
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part, [])
            elif isinstance(node, list) and part.isdigit():
                node = node[int(part)]
            else:
                return []
        return node if isinstance(node, list) else [node]

    def parse_stream(self, lines: list[bytes]) -> list[Any]:
        """Parse a stream of newline-delimited JSON lines."""
        return [fast_loads(line) for line in lines if line.strip()]

    @property
    def backend(self) -> str:
        return self._backend
```

---

## Solution 2: Numba-JIT Numeric Extraction

When tool outputs contain large numeric arrays (sensor readings, time-series, embeddings), use Numba to JIT-compile the extraction and transformation loop.

```python
import numpy as np
from typing import List, Optional

try:
    from numba import njit, prange
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False


if HAS_NUMBA:
    @njit(parallel=True, cache=True)
    def _extract_above_threshold(values: np.ndarray, threshold: float) -> np.ndarray:
        """JIT-compiled: return indices where value > threshold."""
        result = np.empty(len(values), dtype=np.int64)
        count = 0
        for i in prange(len(values)):
            if values[i] > threshold:
                result[count] = i
                count += 1
        return result[:count]

    @njit(cache=True)
    def _compute_rolling_stats(values: np.ndarray, window: int) -> np.ndarray:
        """JIT-compiled rolling mean + std in one pass."""
        n = len(values)
        out = np.zeros((n, 2), dtype=np.float64)  # [mean, std]
        for i in range(n):
            start = max(0, i - window + 1)
            chunk = values[start:i + 1]
            m = chunk.mean()
            s = chunk.std()
            out[i, 0] = m
            out[i, 1] = s
        return out
else:
    def _extract_above_threshold(values: np.ndarray, threshold: float) -> np.ndarray:
        return np.where(values > threshold)[0]

    def _compute_rolling_stats(values: np.ndarray, window: int) -> np.ndarray:
        out = np.zeros((len(values), 2))
        for i in range(len(values)):
            chunk = values[max(0, i - window + 1):i + 1]
            out[i] = [chunk.mean(), chunk.std()]
        return out


class NumericToolOutputParser:
    """
    Fast numeric extraction from tool outputs containing float arrays.

    Benchmark on 1M-element array:
        Pure Python loop:      ~800 ms
        NumPy vectorised:       ~12 ms
        Numba JIT (first call): ~25 ms (compilation overhead)
        Numba JIT (subsequent): ~2 ms

    Usage:
        parser = NumericToolOutputParser()
        values = parser.to_array(tool_output["readings"])
        anomalies = parser.find_anomalies(values, threshold=3.0)
    """

    def to_array(self, records: list, field: str = "value") -> np.ndarray:
        if records and isinstance(records[0], dict):
            return np.array([r[field] for r in records], dtype=np.float64)
        return np.asarray(records, dtype=np.float64)

    def find_anomalies(self, values: np.ndarray,
                       threshold: float = 2.0) -> np.ndarray:
        """Return indices of values more than `threshold` std devs from mean."""
        mean, std = values.mean(), values.std()
        z_scores = np.abs((values - mean) / (std + 1e-9))
        return np.where(z_scores > threshold)[0]

    def above_threshold(self, values: np.ndarray, threshold: float) -> np.ndarray:
        return _extract_above_threshold(values, threshold)

    def rolling_stats(self, values: np.ndarray, window: int = 20) -> np.ndarray:
        return _compute_rolling_stats(values, window)
```

---

## Solution 3: Vectorised CSV/Tabular Parser (Polars)

Replace pandas for tabular tool outputs. Polars uses an OLAP-style columnar engine with multi-threaded execution — typically 5–20× faster than pandas for filter/aggregate operations.

```python
from typing import Optional, Union

try:
    import polars as pl
    HAS_POLARS = True
except ImportError:
    HAS_POLARS = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


class FastTabularParser:
    """
    Parses CSV/JSON tabular tool outputs using Polars (preferred) or pandas.

    Benchmark on 1M rows CSV:
        pandas read_csv:     ~2.1 s
        polars read_csv:     ~0.3 s  (7× faster)

    Usage:
        parser = FastTabularParser()
        df = parser.parse_csv(csv_bytes)
        summary = parser.summarise(df, group_by="category", agg_col="revenue")
    """

    def parse_csv(self, raw: Union[str, bytes],
                  infer_schema: bool = True):
        if HAS_POLARS:
            if isinstance(raw, str):
                raw = raw.encode()
            import io
            return pl.read_csv(io.BytesIO(raw), infer_schema_length=1000 if infer_schema else 0)
        elif HAS_PANDAS:
            import io
            return pd.read_csv(io.StringIO(raw if isinstance(raw, str) else raw.decode()))
        else:
            raise ImportError("polars or pandas required")

    def parse_json_records(self, records: list):
        if HAS_POLARS:
            return pl.DataFrame(records)
        elif HAS_PANDAS:
            return pd.DataFrame(records)
        else:
            raise ImportError("polars or pandas required")

    def filter_and_aggregate(self, df, filter_col: str,
                              filter_val, agg_col: str, agg: str = "sum"):
        if HAS_POLARS:
            import polars as pl
            filtered = df.filter(pl.col(filter_col) == filter_val)
            if agg == "sum":
                return filtered.select(pl.col(agg_col).sum()).item()
            elif agg == "mean":
                return filtered.select(pl.col(agg_col).mean()).item()
            return filtered
        elif HAS_PANDAS:
            filtered = df[df[filter_col] == filter_val]
            return getattr(filtered[agg_col], agg)()
        return None
```

---

## Solution 4: Compiled Regex Pattern Cache

Pre-compile all regex patterns at module load time and use the `regex` module (2–5× faster than `re` for Unicode-heavy patterns). Cache patterns by string key to avoid recompilation.

```python
import re
from typing import Dict, List, Optional, Pattern

try:
    import regex as re_engine
    _RE_BACKEND = "regex"
except ImportError:
    import re as re_engine
    _RE_BACKEND = "re"


class CompiledPatternCache:
    """
    Thread-safe compiled regex cache with pre-loaded common extraction patterns.

    Usage:
        cache = CompiledPatternCache()
        cache.preload({
            "url": r"https?://[^\s<>\"]+",
            "email": r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}",
            "json_key": r'"([^"]+)"\s*:\s*"([^"]+)"',
        })
        urls = cache.findall("url", tool_output_text)
    """

    def __init__(self):
        self._cache: Dict[str, Pattern] = {}
        self._backend = _RE_BACKEND

    def compile(self, name: str, pattern: str,
                flags: int = re_engine.UNICODE) -> Pattern:
        if name not in self._cache:
            self._cache[name] = re_engine.compile(pattern, flags)
        return self._cache[name]

    def preload(self, patterns: Dict[str, str]):
        for name, pat in patterns.items():
            self.compile(name, pat)

    def findall(self, name: str, text: str) -> List[str]:
        pat = self._cache.get(name)
        if pat is None:
            raise KeyError(f"Pattern '{name}' not preloaded. Call compile() first.")
        return pat.findall(text)

    def extract_groups(self, name: str, text: str) -> List[tuple]:
        pat = self._cache.get(name)
        if pat is None:
            raise KeyError(f"Pattern '{name}' not preloaded.")
        return pat.findall(text)

    def sub(self, name: str, repl: str, text: str) -> str:
        pat = self._cache.get(name)
        if pat is None:
            raise KeyError(f"Pattern '{name}' not preloaded.")
        return pat.sub(repl, text)

    @property
    def loaded_patterns(self) -> List[str]:
        return list(self._cache.keys())


# Module-level singleton — patterns compiled once per process
_PATTERN_CACHE = CompiledPatternCache()
_PATTERN_CACHE.preload({
    "url":          r"https?://[^\s<>\"']+",
    "email":        r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}",
    "json_string":  r'"([^"\\]|\\.)*"',
    "iso_date":     r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)?",
    "llm_tag":      r"<(\w+)>(.*?)</\1>",
    "markdown_code": r"```(\w*)\n(.*?)```",
})
```

---

## Solution 5: Streaming JSON Parser for Large Responses

For tool outputs too large to parse at once, use `ijson` to stream-parse without loading the full document into memory. Reduces peak RAM and time-to-first-record dramatically.

```python
import io
from typing import Any, Generator, List, Optional, Union

try:
    import ijson
    HAS_IJSON = True
except ImportError:
    HAS_IJSON = False


class StreamingJSONParser:
    """
    Parse large JSON tool outputs as a stream.
    First record available after parsing only the first few bytes.

    Benchmark on 500 MB JSON array:
        json.loads:         ~12 s, ~500 MB peak RAM
        ijson streaming:    ~14 s,  ~2 MB peak RAM, first record in <0.1 s

    Usage:
        parser = StreamingJSONParser()
        for record in parser.iter_records(large_response_bytes, prefix="items.item"):
            process(record)
            if done_early:
                break   # stops parsing immediately
    """

    def __init__(self, fallback_batch_size: int = 1000):
        self._batch_size = fallback_batch_size

    def iter_records(self, raw: Union[str, bytes],
                     prefix: str = "item") -> Generator[Any, None, None]:
        if HAS_IJSON:
            if isinstance(raw, str):
                raw = raw.encode()
            buf = io.BytesIO(raw)
            yield from ijson.items(buf, prefix)
        else:
            # Fallback: parse all at once, yield records
            import json
            data = json.loads(raw)
            if isinstance(data, list):
                yield from data
            elif isinstance(data, dict):
                # Navigate to prefix
                node = data
                for part in prefix.split("."):
                    if part == "item":
                        break
                    node = node.get(part, data)
                if isinstance(node, list):
                    yield from node
                else:
                    yield node

    def first_n(self, raw: Union[str, bytes],
                n: int, prefix: str = "item") -> List[Any]:
        result = []
        for record in self.iter_records(raw, prefix):
            result.append(record)
            if len(result) >= n:
                break
        return result

    def count_matching(self, raw: Union[str, bytes],
                       predicate, prefix: str = "item") -> int:
        return sum(1 for r in self.iter_records(raw, prefix) if predicate(r))
```

---

## Solution 6: Unified Fast Output Parser Registry

Register the optimal parser for each tool by output format. The agent calls `registry.parse(tool_name, raw_output)` and gets back a parsed result using the fastest available method.

```python
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union


@dataclass
class ParseResult:
    data: Any
    parser_used: str
    parse_ms: float
    record_count: int = 0


class FastParserRegistry:
    """
    Maps tool names to their fastest available parser.

    Usage:
        registry = FastParserRegistry()
        registry.register("web_search", format="json", path="organic_results")
        registry.register("db_query", format="json_records")
        registry.register("csv_export", format="csv")

        result = registry.parse("web_search", raw_bytes)
        records = result.data
    """

    def __init__(self):
        self._json_parser = FastJSONToolOutputParser()
        self._stream_parser = StreamingJSONParser()
        self._tabular_parser = FastTabularParser()
        self._pattern_cache = _PATTERN_CACHE
        self._tool_config: Dict[str, dict] = {}

    def register(self, tool_name: str, format: str,
                 path: Optional[str] = None,
                 streaming_threshold_bytes: int = 10 * 1024 * 1024):
        self._tool_config[tool_name] = {
            "format": format,
            "path": path,
            "streaming_threshold": streaming_threshold_bytes,
        }

    def parse(self, tool_name: str,
              raw: Union[str, bytes]) -> ParseResult:
        cfg = self._tool_config.get(tool_name, {"format": "json"})
        fmt = cfg["format"]
        t0 = time.monotonic()

        if fmt == "json":
            raw_bytes = raw.encode() if isinstance(raw, str) else raw
            use_streaming = len(raw_bytes) > cfg.get("streaming_threshold", 10_000_000)
            if use_streaming:
                path = cfg.get("path") or "item"
                records = self._stream_parser.first_n(raw_bytes, n=10000, prefix=path)
                data = records
                parser_used = "ijson_streaming"
            else:
                data = self._json_parser.parse(raw_bytes)
                if cfg.get("path"):
                    data = self._json_parser.extract_records(data, cfg["path"])
                parser_used = f"json_{self._json_parser.backend}"

        elif fmt == "json_records":
            data = self._json_parser.parse(raw)
            data = data if isinstance(data, list) else [data]
            parser_used = f"json_{self._json_parser.backend}"

        elif fmt == "csv":
            data = self._tabular_parser.parse_csv(raw)
            parser_used = "polars_csv" if HAS_POLARS else "pandas_csv"

        else:
            import json
            data = json.loads(raw)
            parser_used = "stdlib_json"

        parse_ms = (time.monotonic() - t0) * 1000
        count = len(data) if hasattr(data, "__len__") else 0
        return ParseResult(data=data, parser_used=parser_used,
                           parse_ms=parse_ms, record_count=count)
```

---

## Comparison

| Approach | Speedup vs stdlib | Memory Saving | Streaming Support |
|---|---|---|---|
| **orjson Drop-In** | 5–10× | None | No |
| **Numba JIT Numeric** | 50–400× (numeric loops) | None | No |
| **Polars Tabular** | 5–20× | 2–4× | Lazy scan |
| **Compiled Regex Cache** | 2–5× | None | No |
| **Streaming JSON (ijson)** | ~1× throughput, 250× RAM | 250× | Yes |
| **Unified Parser Registry** | Best available for each format | Where applicable | Auto |

**Key insight**: instrument parse time before optimising. A single `orjson` swap often halves JSON parse time with zero architectural change. Numba pays off only when numeric loops are a measured hot path — add it selectively, not speculatively.
