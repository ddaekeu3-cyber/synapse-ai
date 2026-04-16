---
title: "Agent Doesn't Implement Apache Arrow Columnar Format for Large Datasets"
description: "AI agents that process large tabular datasets with row-oriented formats (CSV, JSON lists, pandas DataFrames backed by numpy) waste memory and CPU on repeated serialisation, type inference, and cache-unfriendly row scans. Apache Arrow's columnar in-memory format enables zero-copy inter-process transfer, SIMD-friendly batch operations, and direct integration with DuckDB, Polars, and vectorised LLM context builders."
date: 2025-02-07
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-apache-arrow-columnar-format-for-large-datasets
tags:
  - apache-arrow
  - columnar
  - pyarrow
  - polars
  - duckdb
  - zero-copy
  - large-datasets
  - performance
symptoms:
  - "Agent spends >30% of wall time converting between pandas DataFrames and JSON"
  - "Loading 10M row CSV into agent context causes OOM despite data fitting on disk"
  - "Inter-process data transfer serialises to bytes then deserialises — round-trip dominates latency"
  - "Vectorised column operations are slower than expected because pandas uses object dtype"
  - "Agent passes large tables to tools as JSON strings; tokens explode with column repetition"
---

## Problem

Row-oriented formats require reading every field of every row even when only two columns are needed. Repeated JSON/CSV serialisation for inter-tool transfer adds O(n) CPU work per hop. Pandas with object dtype falls back to Python-speed loops. Arrow fixes all three: columns are contiguous typed buffers that CPUs can SIMD-scan; the format is a zero-copy IPC standard; schema is declared once.

---

## Solution 1: ArrowTableBuilder — Schema-Typed Batch Ingestion

```python
import pyarrow as pa
import pyarrow.compute as pc
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ColumnSpec:
    name: str
    dtype: pa.DataType
    nullable: bool = True


class ArrowTableBuilder:
    """
    Builds an Arrow Table from row-oriented agent tool results
    using a pre-declared schema (no per-row type inference).

    Usage:
        builder = ArrowTableBuilder([
            ColumnSpec("user_id",   pa.int64()),
            ColumnSpec("query",     pa.large_utf8()),
            ColumnSpec("score",     pa.float32()),
            ColumnSpec("ts",        pa.timestamp("ms")),
        ])
        for row in tool_results:
            builder.append(row)
        table = builder.build()
        print(table.schema)
        print(pc.mean(table.column("score")))
    """

    def __init__(self, columns: List[ColumnSpec]):
        self._schema = pa.schema([
            pa.field(c.name, c.dtype, nullable=c.nullable)
            for c in columns
        ])
        self._buffers: Dict[str, list] = {c.name: [] for c in columns}

    def append(self, row: Dict[str, Any]):
        for name, buf in self._buffers.items():
            buf.append(row.get(name))

    def append_many(self, rows: List[Dict[str, Any]]):
        for row in rows:
            self.append(row)

    def build(self) -> pa.Table:
        arrays = {
            name: pa.array(buf, type=self._schema.field(name).type)
            for name, buf in self._buffers.items()
        }
        return pa.table(arrays, schema=self._schema)

    def reset(self):
        for buf in self._buffers.values():
            buf.clear()

    @staticmethod
    def from_pandas(df, schema: Optional[pa.Schema] = None) -> pa.Table:
        return pa.Table.from_pandas(df, schema=schema, preserve_index=False)
```

---

## Solution 2: Zero-Copy IPC for Inter-Tool Transfer

Replace JSON serialisation between agent tools with Arrow IPC streams. The receiver gets a zero-copy view of the same memory buffer.

```python
import io
import pyarrow as pa
import pyarrow.ipc as ipc
from typing import Optional


class ArrowIPCTransport:
    """
    Serialise/deserialise Arrow Tables via IPC for inter-tool transfer.
    Avoids JSON round-trips; the wire format preserves schema and types exactly.

    Usage:
        transport = ArrowIPCTransport()

        # Tool A produces data
        table = pa.table({"x": [1, 2, 3], "y": [4.0, 5.0, 6.0]})
        payload = transport.serialise(table)          # bytes

        # Tool B receives payload (zero-copy if using shared memory)
        received = transport.deserialise(payload)
        assert received.equals(table)
    """

    def serialise(self, table: pa.Table,
                  compression: Optional[str] = "lz4") -> bytes:
        buf = io.BytesIO()
        opts = ipc.IpcWriteOptions(compression=compression)
        with ipc.new_stream(buf, table.schema, options=opts) as writer:
            writer.write_table(table)
        return buf.getvalue()

    def deserialise(self, data: bytes) -> pa.Table:
        buf = io.BytesIO(data)
        reader = ipc.open_stream(buf)
        return reader.read_all()

    def serialise_file(self, table: pa.Table, path: str,
                       compression: Optional[str] = "lz4"):
        """Write Arrow IPC file format (random-access) to disk."""
        opts = ipc.IpcWriteOptions(compression=compression)
        with ipc.new_file(path, table.schema, options=opts) as writer:
            writer.write_table(table, max_chunksize=65536)

    def deserialise_file(self, path: str) -> pa.Table:
        with ipc.open_file(path) as reader:
            return reader.read_all()

    def size_comparison(self, table: pa.Table) -> dict:
        import json
        arrow_bytes = len(self.serialise(table))
        json_bytes = len(json.dumps(table.to_pydict()).encode())
        return {
            "arrow_ipc_bytes": arrow_bytes,
            "json_bytes": json_bytes,
            "ratio": round(json_bytes / arrow_bytes, 2),
        }
```

---

## Solution 3: DuckDB Arrow Query Engine for Agent Analytics

Run SQL directly on Arrow Tables via DuckDB's zero-copy Arrow integration. No data movement between Arrow and DuckDB.

```python
import pyarrow as pa
import duckdb
from typing import Any, Dict, List, Optional


class ArrowDuckDBQueryEngine:
    """
    Executes SQL queries on Arrow Tables using DuckDB's zero-copy Arrow API.
    Results are returned as Arrow Tables for zero-copy downstream processing.

    Usage:
        engine = ArrowDuckDBQueryEngine()
        engine.register("events", events_table)
        engine.register("users",  users_table)

        result = engine.query('''
            SELECT u.name, COUNT(*) AS n, AVG(e.score) AS avg_score
            FROM events e JOIN users u ON e.user_id = u.id
            WHERE e.ts >= '2025-01-01'
            GROUP BY u.name
            ORDER BY avg_score DESC
            LIMIT 20
        ''')
        print(result.schema)
    """

    def __init__(self, memory_limit: str = "4GB", threads: int = 4):
        self._conn = duckdb.connect()
        self._conn.execute(f"SET memory_limit='{memory_limit}'")
        self._conn.execute(f"SET threads={threads}")
        self._registered: Dict[str, pa.Table] = {}

    def register(self, name: str, table: pa.Table):
        self._conn.register(name, table)
        self._registered[name] = table

    def query(self, sql: str) -> pa.Table:
        return self._conn.execute(sql).arrow()

    def query_to_pandas(self, sql: str):
        return self._conn.execute(sql).df()

    def aggregate(self, table_name: str,
                  group_by: List[str],
                  agg_exprs: List[str]) -> pa.Table:
        groups = ", ".join(group_by)
        aggs = ", ".join(agg_exprs)
        return self.query(f"SELECT {groups}, {aggs} FROM {table_name} GROUP BY {groups}")

    def filter_and_select(self, table: pa.Table,
                          where: str,
                          columns: Optional[List[str]] = None) -> pa.Table:
        self._conn.register("_tmp", table)
        cols = ", ".join(columns) if columns else "*"
        result = self._conn.execute(
            f"SELECT {cols} FROM _tmp WHERE {where}"
        ).arrow()
        self._conn.unregister("_tmp")
        return result

    def close(self):
        self._conn.close()
```

---

## Solution 4: Streaming Arrow Reader for Large CSV/Parquet Files

Process files larger than RAM using Arrow's chunked streaming reader. Each batch is processed and released before the next is read.

```python
import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
import pyarrow.compute as pc
from pathlib import Path
from typing import AsyncGenerator, Callable, Generator, Optional


class StreamingArrowReader:
    """
    Reads large CSV or Parquet files in Arrow RecordBatch chunks.
    Each batch fits in memory; processed results are aggregated without
    loading the whole file.

    Usage:
        reader = StreamingArrowReader(chunk_size=100_000)
        total_score = 0.0
        count = 0
        for batch in reader.read_csv("large_events.csv"):
            scores = batch.column("score").to_pylist()
            total_score += sum(scores)
            count += len(scores)
        print(total_score / count)
    """

    def __init__(self, chunk_size: int = 100_000):
        self._chunk_size = chunk_size

    def read_csv(self, path: str,
                 schema: Optional[pa.Schema] = None) -> Generator[pa.RecordBatch, None, None]:
        read_opts = pa_csv.ReadOptions(block_size=self._chunk_size * 100)
        convert_opts = pa_csv.ConvertOptions(
            column_types=dict(zip(schema.names, schema.types)) if schema else None
        )
        with pa_csv.open_csv(path, read_options=read_opts,
                              convert_options=convert_opts) as reader:
            for batch in reader:
                yield batch

    def read_parquet(self, path: str,
                     columns: Optional[list] = None) -> Generator[pa.RecordBatch, None, None]:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=self._chunk_size, columns=columns):
            yield batch

    def map_reduce(self, path: str,
                   map_fn: Callable[[pa.RecordBatch], pa.RecordBatch],
                   reduce_fn: Callable[[pa.Table], pa.Table],
                   fmt: str = "parquet") -> pa.Table:
        batches = []
        reader = self.read_parquet if fmt == "parquet" else self.read_csv
        for batch in reader(path):
            mapped = map_fn(batch)
            batches.append(mapped)
        combined = pa.Table.from_batches(batches)
        return reduce_fn(combined)

    def count_rows(self, path: str, fmt: str = "parquet") -> int:
        n = 0
        reader = self.read_parquet if fmt == "parquet" else self.read_csv
        for batch in reader(path):
            n += batch.num_rows
        return n
```

---

## Solution 5: Arrow-Based LLM Context Builder

Converts columnar data into token-efficient LLM context strings. Serialises only the rows and columns relevant to the query, respecting token budgets.

```python
import pyarrow as pa
import pyarrow.compute as pc
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ContextChunk:
    text: str
    row_start: int
    row_end: int
    columns_included: List[str]
    estimated_tokens: int


class ArrowLLMContextBuilder:
    """
    Builds token-efficient context strings from Arrow Tables.
    Supports column selection, row filtering, and token-budget enforcement.

    Usage:
        builder = ArrowLLMContextBuilder(token_budget=4096)
        chunks = builder.build_context(
            table,
            relevant_columns=["name", "revenue", "region"],
            filter_expr=pc.greater(table["revenue"], 1_000_000),
            format="markdown",
        )
        for chunk in chunks:
            llm_messages.append({"role": "tool", "content": chunk.text})
    """

    CHARS_PER_TOKEN = 4  # rough estimate

    def __init__(self, token_budget: int = 4096):
        self._budget = token_budget

    def build_context(self,
                      table: pa.Table,
                      relevant_columns: Optional[List[str]] = None,
                      filter_expr=None,
                      format: str = "markdown") -> List[ContextChunk]:
        if filter_expr is not None:
            mask = filter_expr
            table = table.filter(mask)

        if relevant_columns:
            available = [c for c in relevant_columns if c in table.schema.names]
            table = table.select(available)

        rows_per_chunk = max(1, (self._budget * self.CHARS_PER_TOKEN) // (
            len(table.schema.names) * 20  # avg 20 chars per cell
        ))

        chunks = []
        for start in range(0, table.num_rows, rows_per_chunk):
            end = min(start + rows_per_chunk, table.num_rows)
            slice_tbl = table.slice(start, end - start)
            text = self._format_table(slice_tbl, format)
            chunks.append(ContextChunk(
                text=text,
                row_start=start,
                row_end=end,
                columns_included=table.schema.names,
                estimated_tokens=len(text) // self.CHARS_PER_TOKEN,
            ))
        return chunks

    def _format_table(self, table: pa.Table, format: str) -> str:
        if format == "markdown":
            header = "| " + " | ".join(table.schema.names) + " |"
            sep = "| " + " | ".join(["---"] * len(table.schema.names)) + " |"
            rows = []
            for i in range(table.num_rows):
                cells = [str(table.column(c)[i].as_py()) for c in table.schema.names]
                rows.append("| " + " | ".join(cells) + " |")
            return "\n".join([header, sep] + rows)
        elif format == "csv":
            lines = [",".join(table.schema.names)]
            for i in range(table.num_rows):
                cells = [str(table.column(c)[i].as_py()) for c in table.schema.names]
                lines.append(",".join(cells))
            return "\n".join(lines)
        else:
            return table.to_pydict().__repr__()
```

---

## Solution 6: ArrowAgentMemoryStore — Columnar Episodic Memory

Store agent episodic memory as an Arrow Table for fast similarity-filtered recall without loading everything into a vector DB.

```python
import time
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.compute as pc


class ArrowAgentMemoryStore:
    """
    Columnar episodic memory for agents backed by Arrow Table.
    Supports fast column-filter recall (by session, score, recency).
    Persists to Parquet; reloads zero-copy on restart.

    Usage:
        store = ArrowAgentMemoryStore()
        store.record(session_id="s1", role="user",
                     content="What is the capital of France?",
                     importance=0.9)
        recent = store.recall(session_id="s1", limit=20)
        important = store.recall_by_importance(threshold=0.8)
        store.save("memory.parquet")
        store.load("memory.parquet")
    """

    SCHEMA = pa.schema([
        pa.field("session_id",  pa.large_utf8()),
        pa.field("role",        pa.dictionary(pa.int8(), pa.utf8())),
        pa.field("content",     pa.large_utf8()),
        pa.field("importance",  pa.float32()),
        pa.field("ts",          pa.float64()),
    ])

    def __init__(self):
        self._rows: List[Dict[str, Any]] = []
        self._table: Optional[pa.Table] = None

    def record(self, session_id: str, role: str,
               content: str, importance: float = 0.5):
        self._rows.append({
            "session_id": session_id,
            "role": role,
            "content": content,
            "importance": importance,
            "ts": time.time(),
        })
        self._table = None  # invalidate cache

    def _get_table(self) -> pa.Table:
        if self._table is None:
            self._table = pa.Table.from_pylist(self._rows, schema=self.SCHEMA)
        return self._table

    def recall(self, session_id: str, limit: int = 50) -> pa.Table:
        tbl = self._get_table()
        filtered = tbl.filter(pc.equal(tbl["session_id"], session_id))
        sorted_tbl = filtered.sort_by([("ts", "descending")])
        return sorted_tbl.slice(0, limit)

    def recall_by_importance(self, threshold: float = 0.7) -> pa.Table:
        tbl = self._get_table()
        return tbl.filter(pc.greater_equal(tbl["importance"], threshold))

    def save(self, path: str):
        import pyarrow.parquet as pq
        pq.write_table(self._get_table(), path, compression="snappy")

    def load(self, path: str):
        import pyarrow.parquet as pq
        self._table = pq.read_table(path)
        self._rows = self._table.to_pylist()

    def stats(self) -> Dict[str, Any]:
        tbl = self._get_table()
        return {
            "total_entries": tbl.num_rows,
            "sessions": tbl.column("session_id").unique().to_pylist(),
            "avg_importance": pc.mean(tbl.column("importance")).as_py(),
            "memory_bytes": tbl.get_total_buffer_size(),
        }
```

---

## Comparison

| Approach | Use Case | Zero-Copy | Streaming | SQL Support |
|---|---|---|---|---|
| **ArrowTableBuilder** | Ingestion from row results | Partial | No | No |
| **ArrowIPCTransport** | Inter-tool data transfer | Yes | No | No |
| **DuckDB Query Engine** | Analytics on Arrow Tables | Yes | No | Yes |
| **StreamingArrowReader** | Files larger than RAM | No | Yes | No |
| **LLM Context Builder** | Token-budget context windows | No | No | No |
| **ArrowAgentMemoryStore** | Columnar episodic memory | Yes (Parquet reload) | No | No |

**Key insight**: register Arrow Tables directly with DuckDB — no copy, no serialisation. For files larger than RAM use the streaming reader with map/reduce. For inter-tool transfer replace JSON strings with Arrow IPC; size ratios of 5–20× are typical on numeric data.
