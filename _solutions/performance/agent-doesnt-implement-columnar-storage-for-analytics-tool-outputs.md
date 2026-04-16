---
title: "Agent Doesn't Implement Columnar Storage for Analytics Tool Outputs"
description: "Agents that return large tabular datasets as JSON row arrays waste memory and make downstream analytics slow. Implement Apache Arrow and Parquet columnar storage to reduce memory footprint, enable vectorized processing, and accelerate aggregation queries on tool outputs."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-columnar-storage-for-analytics-tool-outputs
tags: [columnar-storage, apache-arrow, parquet, performance, analytics, memory-efficiency]
symptoms:
  - "Agent returns 50,000-row JSON array causing 400MB memory spike in context"
  - "Pandas read_json on tool output 10x slower than equivalent CSV read"
  - "Aggregation queries (GROUP BY, SUM) on tool results scan every row individually"
  - "Large dataset tool responses exceed context window even with summarization"
  - "Tool output serialization/deserialization dominates agent turn latency"
---

## Why This Happens

Row-oriented JSON is the default output format for agent tools because it's human-readable and easy to produce. But for analytical workloads — aggregations, column scans, statistical summaries — columnar formats like Apache Arrow and Parquet are 10–100x more efficient. A column store keeps all values for one attribute contiguous in memory, enabling vectorized SIMD operations, efficient compression, and zero-copy slicing. For agents that process large datasets, switching tool output to columnar format dramatically reduces memory and processing time.

## Solution 1: Arrow Table Builder for Tool Output

```python
import pyarrow as pa
import pyarrow.compute as pc
from typing import Any, Dict, List, Optional

class ArrowTableBuilder:
    """
    Converts tool output (list of row dicts) to an Apache Arrow Table.
    Arrow Tables enable zero-copy slicing, vectorized compute, and
    efficient serialization to Parquet or IPC format.
    """

    def from_rows(self, rows: List[Dict[str, Any]]) -> pa.Table:
        if not rows:
            return pa.table({})
        return pa.Table.from_pylist(rows)

    def from_schema_rows(
        self,
        rows: List[Dict[str, Any]],
        schema: pa.Schema,
    ) -> pa.Table:
        """Build with explicit schema for type safety and memory efficiency."""
        arrays = {}
        for field in schema:
            col_values = [row.get(field.name) for row in rows]
            arrays[field.name] = pa.array(col_values, type=field.type)
        return pa.table(arrays, schema=schema)

    def serialize_ipc(self, table: pa.Table) -> bytes:
        """Serialize to Arrow IPC format (fast, zero-copy on read)."""
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return sink.getvalue().to_pybytes()

    def deserialize_ipc(self, data: bytes) -> pa.Table:
        buf = pa.py_buffer(data)
        reader = pa.ipc.open_stream(buf)
        return reader.read_all()

    def to_summary(self, table: pa.Table, max_preview_rows: int = 5) -> dict:
        """Return a compact summary suitable for LLM context injection."""
        return {
            "rows": table.num_rows,
            "columns": table.column_names,
            "schema": {f.name: str(f.type) for f in table.schema},
            "preview": table.slice(0, max_preview_rows).to_pydict(),
            "memory_bytes": table.get_total_buffer_size(),
        }


# Predefined schema for common tool output types
ORDER_SCHEMA = pa.schema([
    pa.field("order_id", pa.int64()),
    pa.field("user_id", pa.int64()),
    pa.field("amount", pa.float64()),
    pa.field("status", pa.dictionary(pa.int8(), pa.utf8())),
    pa.field("created_at", pa.timestamp("ms")),
])
```

## Solution 2: Parquet Writer and Reader for Tool Result Caching

```python
import io
import pyarrow as pa
import pyarrow.parquet as pq
from typing import Optional

class ParquetToolResultCache:
    """
    Caches large tool results as Parquet files in object storage or local disk.
    Subsequent calls for the same query read from the Parquet cache instead of
    re-querying the source database.
    """

    def __init__(self, storage_backend, compression: str = "snappy"):
        self._storage = storage_backend
        self._compression = compression

    def _cache_key(self, tool_name: str, params_hash: str) -> str:
        return f"tool_cache/{tool_name}/{params_hash}.parquet"

    async def get(self, tool_name: str, params_hash: str) -> Optional[pa.Table]:
        key = self._cache_key(tool_name, params_hash)
        data = await self._storage.get(key)
        if data is None:
            return None
        return pq.read_table(io.BytesIO(data))

    async def put(self, tool_name: str, params_hash: str, table: pa.Table) -> None:
        key = self._cache_key(tool_name, params_hash)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression=self._compression)
        await self._storage.put(key, buf.getvalue())

    def write_parquet(self, table: pa.Table) -> bytes:
        buf = io.BytesIO()
        pq.write_table(
            table, buf,
            compression=self._compression,
            use_dictionary=True,
            write_statistics=True,
        )
        return buf.getvalue()

    def read_parquet(self, data: bytes, columns: Optional[list] = None) -> pa.Table:
        """Column projection pushdown — only read requested columns."""
        return pq.read_table(io.BytesIO(data), columns=columns)

    def parquet_metadata(self, data: bytes) -> dict:
        meta = pq.read_metadata(io.BytesIO(data))
        return {
            "num_rows": meta.num_rows,
            "num_columns": meta.num_row_groups,
            "row_groups": meta.num_row_groups,
            "serialized_size": len(data),
        }
```

## Solution 3: Vectorized Aggregation on Arrow Tables

```python
import pyarrow as pa
import pyarrow.compute as pc
from typing import Any, Dict, List, Optional

class ArrowAggregator:
    """
    Performs common aggregations on Arrow Tables using vectorized compute kernels.
    10–50x faster than Python-loop aggregation on large datasets.
    """

    def group_by_sum(
        self, table: pa.Table, group_col: str, agg_col: str
    ) -> pa.Table:
        return (
            table.group_by(group_col)
            .aggregate([(agg_col, "sum")])
        )

    def group_by_mean(
        self, table: pa.Table, group_col: str, agg_col: str
    ) -> pa.Table:
        return (
            table.group_by(group_col)
            .aggregate([(agg_col, "mean")])
        )

    def filter_rows(self, table: pa.Table, col: str, op: str, value: Any) -> pa.Table:
        """Filter table rows: op is one of '>', '<', '=', '!=', '>=', '<='."""
        ops = {
            ">": pc.greater,
            "<": pc.less,
            "=": pc.equal,
            "!=": pc.not_equal,
            ">=": pc.greater_equal,
            "<=": pc.less_equal,
        }
        fn = ops.get(op)
        if fn is None:
            raise ValueError(f"Unknown operator: {op}")
        mask = fn(table.column(col), pa.scalar(value))
        return table.filter(mask)

    def describe(self, table: pa.Table, col: str) -> dict:
        arr = table.column(col)
        return {
            "count": pc.count(arr).as_py(),
            "sum": pc.sum(arr).as_py(),
            "mean": pc.mean(arr).as_py(),
            "min": pc.min(arr).as_py(),
            "max": pc.max(arr).as_py(),
            "stddev": pc.stddev(arr).as_py(),
        }

    def top_n(self, table: pa.Table, col: str, n: int, ascending: bool = False) -> pa.Table:
        indices = pc.sort_indices(table.column(col), sort_keys=[(col, "ascending" if ascending else "descending")])
        return table.take(indices[:n])
```

## Solution 4: Streaming Parquet Writer for Large Tool Outputs

```python
import pyarrow as pa
import pyarrow.parquet as pq
import io
from typing import Iterator, List

class StreamingParquetWriter:
    """
    Writes large tool outputs to Parquet in row-group batches.
    Avoids materializing the entire dataset in memory at once.
    """

    def __init__(
        self,
        schema: pa.Schema,
        compression: str = "snappy",
        row_group_size: int = 50_000,
    ):
        self._schema = schema
        self._compression = compression
        self._row_group_size = row_group_size
        self._buf = io.BytesIO()
        self._writer: Optional[pq.ParquetWriter] = None

    def __enter__(self):
        self._writer = pq.ParquetWriter(
            self._buf, self._schema, compression=self._compression
        )
        return self

    def __exit__(self, *args):
        if self._writer:
            self._writer.close()

    def write_batch(self, rows: List[dict]) -> None:
        batch = pa.RecordBatch.from_pylist(rows, schema=self._schema)
        self._writer.write_batch(batch)

    def write_stream(self, row_iter: Iterator[dict]) -> None:
        batch = []
        for row in row_iter:
            batch.append(row)
            if len(batch) >= self._row_group_size:
                self.write_batch(batch)
                batch.clear()
        if batch:
            self.write_batch(batch)

    def get_bytes(self) -> bytes:
        return self._buf.getvalue()


class StreamingParquetToolWrapper:
    """Wraps a streaming database cursor and writes output directly to Parquet."""

    def __init__(self, schema: pa.Schema):
        self._schema = schema

    async def cursor_to_parquet(self, cursor, batch_size: int = 10_000) -> bytes:
        with StreamingParquetWriter(self._schema) as writer:
            batch = []
            async for row in cursor:
                batch.append(dict(row))
                if len(batch) >= batch_size:
                    writer.write_batch(batch)
                    batch.clear()
            if batch:
                writer.write_batch(batch)
            return writer.get_bytes()
```

## Solution 5: Arrow-Native Tool Result Slicer for Context Injection

```python
import pyarrow as pa
import pyarrow.compute as pc
from typing import List, Optional

class ContextWindowArrowSlicer:
    """
    Takes a large Arrow table and produces a context-friendly summary
    that fits within the LLM's token budget. Prioritizes columns
    the agent asked about; samples rows if the table is large.
    """

    def __init__(self, max_tokens_estimate: int = 2000):
        self._max_tokens = max_tokens_estimate

    def slice_for_context(
        self,
        table: pa.Table,
        focus_columns: Optional[List[str]] = None,
        sample_rows: int = 20,
    ) -> str:
        # Project only relevant columns
        if focus_columns:
            available = [c for c in focus_columns if c in table.column_names]
            if available:
                table = table.select(available)

        # Sample rows if large
        if table.num_rows > sample_rows:
            step = table.num_rows // sample_rows
            indices = pa.array(range(0, table.num_rows, step)[:sample_rows])
            table = table.take(indices)

        lines = [", ".join(table.column_names)]
        for batch in table.to_batches():
            for row_idx in range(batch.num_rows):
                row_vals = [
                    str(batch.column(i)[row_idx].as_py())
                    for i in range(batch.num_columns)
                ]
                lines.append(", ".join(row_vals))
                if len("\n".join(lines)) > self._max_tokens * 4:  # ~4 chars/token
                    lines.append(f"... ({table.num_rows} total rows)")
                    break

        return "\n".join(lines)
```

## Solution 6: Columnar Tool Benchmark Harness

```python
import time
import json
import pyarrow as pa
import pyarrow.parquet as pq
import io
from typing import List, Dict, Any

class ColumnarBenchmark:
    """Compare JSON row format vs Arrow/Parquet on serialization and query speed."""

    def run(self, rows: List[Dict[str, Any]]) -> dict:
        n = len(rows)
        results = {}

        # JSON baseline
        t0 = time.monotonic()
        json_bytes = json.dumps(rows).encode()
        json_ser_ms = (time.monotonic() - t0) * 1000

        t0 = time.monotonic()
        json.loads(json_bytes)
        json_deser_ms = (time.monotonic() - t0) * 1000

        results["json"] = {
            "size_bytes": len(json_bytes),
            "serialize_ms": round(json_ser_ms, 2),
            "deserialize_ms": round(json_deser_ms, 2),
        }

        # Arrow IPC
        table = pa.Table.from_pylist(rows)
        t0 = time.monotonic()
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as w:
            w.write_table(table)
        arrow_bytes = sink.getvalue().to_pybytes()
        arrow_ser_ms = (time.monotonic() - t0) * 1000

        t0 = time.monotonic()
        pa.ipc.open_stream(pa.py_buffer(arrow_bytes)).read_all()
        arrow_deser_ms = (time.monotonic() - t0) * 1000

        results["arrow_ipc"] = {
            "size_bytes": len(arrow_bytes),
            "serialize_ms": round(arrow_ser_ms, 2),
            "deserialize_ms": round(arrow_deser_ms, 2),
            "size_reduction_vs_json": round(1 - len(arrow_bytes) / len(json_bytes), 3),
        }

        # Parquet
        buf = io.BytesIO()
        t0 = time.monotonic()
        pq.write_table(table, buf, compression="snappy")
        parquet_bytes = buf.getvalue()
        parquet_ser_ms = (time.monotonic() - t0) * 1000

        t0 = time.monotonic()
        pq.read_table(io.BytesIO(parquet_bytes))
        parquet_deser_ms = (time.monotonic() - t0) * 1000

        results["parquet_snappy"] = {
            "size_bytes": len(parquet_bytes),
            "serialize_ms": round(parquet_ser_ms, 2),
            "deserialize_ms": round(parquet_deser_ms, 2),
            "size_reduction_vs_json": round(1 - len(parquet_bytes) / len(json_bytes), 3),
        }

        results["row_count"] = n
        return results
```

## Comparison

| Approach | Memory Efficiency | Query Speed | Streaming | Human-Readable |
|---|---|---|---|---|
| JSON row arrays | Low (all in memory) | Slow (Python loops) | No | Yes |
| ArrowTableBuilder (IPC) | High (columnar) | High (vectorized) | No | No |
| ParquetToolResultCache | Very high (compressed) | High + column projection | No | No |
| ArrowAggregator | High | Very high (SIMD kernels) | No | No |
| StreamingParquetWriter | Very high (batch write) | High | Yes | No |
| ContextWindowArrowSlicer | N/A | N/A | N/A | Yes (text output) |

**Best for production**: Use `StreamingParquetWriter` to write large tool results directly to Parquet without materializing in memory. Use `ParquetToolResultCache` to cache results by query hash. Use `ArrowAggregator` for all aggregation tasks. Use `ContextWindowArrowSlicer` to produce compact text summaries for LLM context injection. Run `ColumnarBenchmark` to quantify savings on your actual workload.
