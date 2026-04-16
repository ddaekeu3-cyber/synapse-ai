---
layout: solution
title: "Agent Doesn't Implement Distributed Log Correlation"
description: "How to propagate correlation IDs across agent calls, tool invocations, and subagents so logs from all components can be joined for a single request."
tags: [observability, logging, correlation-id, tracing, distributed-systems, structured-logging]
difficulty: intermediate
solution_count: 6
---

## Problem

An agent makes LLM API calls, invokes tools, spawns subagents, and writes to databases. Each component logs independently with no shared identifier. When something fails, you have logs from the API layer, tool runners, and subagents — but no way to join them. Debugging requires manual timestamp matching across dozens of log files.

```python
# Bad: every component logs in isolation
logger.info("Agent responded")          # no ID
tool_logger.error("Tool failed")        # different service, no link
subagent_logger.warning("Retry 3")      # third system, no correlation
# These three events are related — but you'll never know from the logs
```

---

## Solution 1 — Request-Scoped Correlation ID with contextvars

Attach a correlation ID to every request at ingress and propagate it automatically through `contextvars.ContextVar`. Any logger can read it without passing it explicitly.

```python
import asyncio
import contextvars
import logging
import uuid
import json
from typing import Any

# Single ContextVar holds the correlation ID for the current async task tree
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="no-correlation"
)

class CorrelatingFormatter(logging.Formatter):
    """Injects correlation_id into every log record."""

    def format(self, record: logging.LogRecord) -> str:
        record.correlation_id = correlation_id.get("no-correlation")
        return super().format(record)

def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(CorrelatingFormatter(
        '{"ts":"%(asctime)s","level":"%(levelname)s","cid":"%(correlation_id)s",'
        '"logger":"%(name)s","msg":%(message)s}'
    ))
    logger = logging.getLogger("agent")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger

logger = setup_logging()

def new_correlation_id() -> str:
    return str(uuid.uuid4())

async def handle_request(user_message: str) -> str:
    """Entry point — set a fresh correlation ID for the entire request."""
    cid = new_correlation_id()
    token = correlation_id.set(cid)
    try:
        logger.info('"Handling request"')
        result = await run_agent(user_message)
        logger.info('"Request complete"')
        return result
    finally:
        correlation_id.reset(token)

async def run_agent(message: str) -> str:
    logger.info(f'"Running agent for message: {message[:50]}"')
    tool_result = await invoke_tool("search", {"query": message})
    return f"Response based on: {tool_result}"

async def invoke_tool(name: str, args: dict) -> Any:
    # correlation_id is automatically available — same ContextVar
    logger.info(f'"Invoking tool {name}"')
    await asyncio.sleep(0.01)
    logger.info(f'"Tool {name} returned"')
    return {"results": []}

# All three log lines share the same cid:
# {"cid":"550e8400-e29b-41d4-a716","msg":"Handling request"}
# {"cid":"550e8400-e29b-41d4-a716","msg":"Running agent for message: hello"}
# {"cid":"550e8400-e29b-41d4-a716","msg":"Invoking tool search"}

asyncio.run(handle_request("hello"))
```

---

## Solution 2 — Correlation ID Propagation via HTTP Headers

When the agent calls external services (APIs, microservices, subagent HTTP endpoints), forward the correlation ID in a standard header so the downstream service can include it in its own logs.

```python
import asyncio
import contextvars
import uuid
import httpx
import logging
from typing import Any

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

CORRELATION_HEADER = "X-Correlation-ID"
REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger("agent.http")

class CorrelatingHTTPClient:
    """httpx client that automatically forwards correlation headers."""

    def __init__(self, base_url: str = ""):
        self._client = httpx.AsyncClient(base_url=base_url)

    def _headers(self) -> dict[str, str]:
        cid = correlation_id.get("")
        headers = {}
        if cid:
            headers[CORRELATION_HEADER] = cid
            headers[REQUEST_ID_HEADER] = f"{cid}-{uuid.uuid4().hex[:8]}"
        return headers

    async def get(self, url: str, **kwargs) -> httpx.Response:
        kwargs.setdefault("headers", {}).update(self._headers())
        logger.info(f"GET {url} cid={correlation_id.get('')}")
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        kwargs.setdefault("headers", {}).update(self._headers())
        logger.info(f"POST {url} cid={correlation_id.get('')}")
        return await self._client.post(url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

# FastAPI middleware to extract correlation ID from incoming requests
# (place this in your agent's HTTP server)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cid = (
            request.headers.get(CORRELATION_HEADER)
            or request.headers.get(REQUEST_ID_HEADER)
            or str(uuid.uuid4())
        )
        token = correlation_id.set(cid)
        try:
            response = await call_next(request)
            response.headers[CORRELATION_HEADER] = cid
            return response
        finally:
            correlation_id.reset(token)

# Usage
async def agent_workflow():
    cid = str(uuid.uuid4())
    correlation_id.set(cid)

    client = CorrelatingHTTPClient("https://api.example.com")
    try:
        # All requests carry X-Correlation-ID automatically
        search_result = await client.get("/search?q=agents")
        embed_result = await client.post("/embed", json={"text": "hello"})
        logger.info(f"Workflow complete. search={search_result.status_code}")
    finally:
        await client.aclose()
```

---

## Solution 3 — Structured Log Sink with Correlation Join Index

Write all logs to a structured JSONL file and build an in-memory index by correlation ID, enabling instant log-join queries during debugging.

```python
import json
import time
import uuid
import contextvars
import logging
from pathlib import Path
from collections import defaultdict
from typing import Iterator

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

class StructuredLogSink:
    """Writes JSONL logs and maintains an in-memory index by correlation ID."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._index: dict[str, list[int]] = defaultdict(list)  # cid -> byte offsets
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, message: str, **extra) -> None:
        cid = correlation_id.get("") or "no-cid"
        entry = {
            "ts": time.time(),
            "level": level,
            "cid": cid,
            "msg": message,
            **extra,
        }
        line = json.dumps(entry) + "\n"

        with open(self._path, "a") as f:
            offset = f.tell() if hasattr(f, "tell") else 0
            f.write(line)

        self._index[cid].append(offset)

    def get_correlated_logs(self, cid: str) -> list[dict]:
        """Return all log entries for a given correlation ID."""
        offsets = self._index.get(cid, [])
        if not offsets:
            # Fallback: full scan (for logs written in a previous process)
            return self._scan_for_cid(cid)
        results = []
        with open(self._path) as f:
            for offset in offsets:
                f.seek(offset)
                results.append(json.loads(f.readline()))
        return results

    def _scan_for_cid(self, cid: str) -> list[dict]:
        results = []
        with open(self._path) as f:
            for line in f:
                entry = json.loads(line)
                if entry.get("cid") == cid:
                    results.append(entry)
        return results

    def tail_by_cid(self, cid: str) -> Iterator[dict]:
        """Stream log entries for a correlation ID as they arrive."""
        seen_offsets = set()
        while True:
            new_offsets = [o for o in self._index.get(cid, []) if o not in seen_offsets]
            with open(self._path) as f:
                for offset in new_offsets:
                    f.seek(offset)
                    yield json.loads(f.readline())
                    seen_offsets.add(offset)
            if not new_offsets:
                break

# Usage
sink = StructuredLogSink("/var/log/agent/agent.jsonl")

async def process_request(msg: str) -> str:
    cid = str(uuid.uuid4())
    correlation_id.set(cid)
    sink.write("INFO", "request started", input_len=len(msg))
    # ... agent work ...
    sink.write("INFO", "llm called", model="claude-haiku-4-5-20251001")
    sink.write("INFO", "request complete")

    # Later: join all logs for this request
    logs = sink.get_correlated_logs(cid)
    print(f"Request had {len(logs)} log entries")
    return "done"
```

---

## Solution 4 — Multi-Service Log Aggregator with Elasticsearch-Style Query

Aggregate logs from multiple agent components (orchestrator, tool runner, subagents) into a single queryable store, joined by correlation ID.

```python
import asyncio
import json
import time
import uuid
import contextvars
from dataclasses import dataclass, field, asdict
from typing import Any

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

@dataclass
class LogEntry:
    ts: float
    cid: str
    service: str
    level: str
    message: str
    metadata: dict = field(default_factory=dict)

class LogAggregator:
    """In-process log store supporting correlation-ID queries."""

    def __init__(self):
        self._entries: list[LogEntry] = []
        self._cid_index: dict[str, list[int]] = {}  # cid -> list of entry indices

    def ingest(self, entry: LogEntry) -> None:
        idx = len(self._entries)
        self._entries.append(entry)
        self._cid_index.setdefault(entry.cid, []).append(idx)

    def query(self, cid: str = None, service: str = None,
              level: str = None, since: float = 0) -> list[LogEntry]:
        if cid:
            indices = self._cid_index.get(cid, [])
            candidates = [self._entries[i] for i in indices]
        else:
            candidates = self._entries

        return [
            e for e in candidates
            if (service is None or e.service == service)
            and (level is None or e.level == level)
            and e.ts >= since
        ]

    def timeline(self, cid: str) -> list[str]:
        """Human-readable timeline for a correlation ID."""
        entries = self.query(cid=cid)
        entries.sort(key=lambda e: e.ts)
        origin = entries[0].ts if entries else time.time()
        return [
            f"+{e.ts - origin:6.3f}s [{e.service:20s}] {e.level:5s} {e.message}"
            for e in entries
        ]

aggregator = LogAggregator()

def make_logger(service: str):
    def log(level: str, message: str, **meta) -> None:
        aggregator.ingest(LogEntry(
            ts=time.time(),
            cid=correlation_id.get(""),
            service=service,
            level=level,
            message=message,
            metadata=meta,
        ))
    return log

orchestrator_log = make_logger("orchestrator")
tool_log = make_logger("tool_runner")
subagent_log = make_logger("subagent")

async def demo():
    cid = str(uuid.uuid4())
    correlation_id.set(cid)

    orchestrator_log("INFO", "request received", user="alice")
    tool_log("INFO", "executing search tool", query="agents")
    subagent_log("INFO", "subagent invoked", model="claude-haiku-4-5-20251001")
    subagent_log("WARNING", "subagent retrying", attempt=2)
    tool_log("INFO", "search complete", result_count=5)
    orchestrator_log("INFO", "response sent")

    print("\n".join(aggregator.timeline(cid)))

asyncio.run(demo())
# +0.000s [orchestrator        ] INFO  request received
# +0.001s [tool_runner         ] INFO  executing search tool
# +0.002s [subagent            ] INFO  subagent invoked
# +0.003s [subagent            ] WARNING subagent retrying
# +0.004s [tool_runner         ] INFO  search complete
# +0.005s [orchestrator        ] INFO  response sent
```

---

## Solution 5 — Correlation ID Propagation into LLM Prompts and Responses

Embed the correlation ID in the system prompt so that if the LLM itself logs reasoning or tool calls, those can be joined with the infrastructure logs.

```python
import asyncio
import contextvars
import uuid
import logging
from anthropic import AsyncAnthropic

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

client = AsyncAnthropic()
logger = logging.getLogger("agent.llm")

SYSTEM_PROMPT_TEMPLATE = """\
You are a helpful assistant.

<!-- trace: cid={cid} -->

Always respond in JSON with keys: "answer" and "reasoning".
"""

async def call_llm_with_correlation(user_message: str) -> dict:
    cid = correlation_id.get("") or str(uuid.uuid4())
    system = SYSTEM_PROMPT_TEMPLATE.format(cid=cid)

    logger.info("LLM call started", extra={"cid": cid, "prompt_len": len(user_message)})

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    output = response.content[0].text
    logger.info("LLM call complete", extra={
        "cid": cid,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "stop_reason": response.stop_reason,
    })

    return {
        "cid": cid,
        "output": output,
        "usage": {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        }
    }

async def correlated_agent_run(user_message: str) -> str:
    cid = str(uuid.uuid4())
    token = correlation_id.set(cid)
    try:
        logger.info("Agent run started", extra={"cid": cid})
        result = await call_llm_with_correlation(user_message)
        # LLM prompt contained cid — any prompt-level logging is now joinable
        logger.info("Agent run complete", extra={"cid": cid})
        return result["output"]
    finally:
        correlation_id.reset(token)

asyncio.run(correlated_agent_run("What is 2+2?"))
```

---

## Solution 6 — Correlation-Aware Log Shipper to Centralized Backend

Ship structured logs with correlation IDs to a centralized backend (e.g., Loki, CloudWatch, Datadog) so cross-service queries work in production.

```python
import asyncio
import json
import time
import uuid
import contextvars
import logging
from collections import deque
from typing import Any
import httpx

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

class BatchLogShipper:
    """Buffers log entries and ships them in batches to a centralized log backend."""

    def __init__(self, endpoint: str, service_name: str,
                 batch_size: int = 50, flush_interval: float = 2.0):
        self._endpoint = endpoint
        self._service = service_name
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: deque[dict] = deque()
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient()
        self._task: asyncio.Task | None = None

    def emit(self, level: str, message: str, **extra) -> None:
        """Non-blocking — appends to buffer for async shipping."""
        entry = {
            "ts": time.time(),
            "service": self._service,
            "level": level,
            "cid": correlation_id.get(""),
            "msg": message,
            **extra,
        }
        self._buffer.append(entry)
        # Immediate flush if buffer is full
        if len(self._buffer) >= self._batch_size and self._task:
            asyncio.ensure_future(self._flush())

    async def _flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch = []
            while self._buffer and len(batch) < self._batch_size:
                batch.append(self._buffer.popleft())

        payload = {
            "streams": [{
                "stream": {"service": self._service},
                "values": [
                    [str(int(e["ts"] * 1e9)), json.dumps(e)]
                    for e in batch
                ]
            }]
        }
        try:
            await self._client.post(
                self._endpoint, json=payload, timeout=5.0
            )
        except Exception as exc:
            # Restore to buffer on failure (best-effort)
            for entry in reversed(batch):
                self._buffer.appendleft(entry)
            logging.getLogger("log_shipper").warning(f"Ship failed: {exc}")

    async def _background_flush(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._background_flush())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self._flush()  # final flush
        await self._client.aclose()

# Usage
async def main():
    shipper = BatchLogShipper(
        endpoint="http://loki:3100/loki/api/v1/push",
        service_name="agent-service",
    )
    await shipper.start()

    # Handle multiple concurrent requests — each gets a unique correlation ID
    async def handle(msg: str) -> None:
        cid = str(uuid.uuid4())
        token = correlation_id.set(cid)
        try:
            shipper.emit("INFO", "request started", input=msg[:50])
            await asyncio.sleep(0.1)  # simulate agent work
            shipper.emit("INFO", "llm called", model="claude-haiku-4-5-20251001")
            await asyncio.sleep(0.05)
            shipper.emit("INFO", "request complete")
        finally:
            correlation_id.reset(token)

    await asyncio.gather(*[handle(f"message {i}") for i in range(10)])
    # In Loki: {service="agent-service"} | json | cid="<cid>" joins all 3 events
    await shipper.stop()

asyncio.run(main())
```

---

## Comparison

| Approach | Cross-Process | HTTP Propagation | Queryable | Production Ready | Best For |
|---|---|---|---|---|---|
| contextvars ContextVar | No (same process) | No | No | Yes | Single-process agents |
| HTTP header forwarding | **Yes** | **Yes** | No | Yes | Microservice architectures |
| Structured JSONL sink | No | No | **Yes** (offset index) | Yes | Offline debugging |
| Multi-service aggregator | No | No | **Yes** | Dev/staging | Local multi-component debugging |
| LLM prompt embedding | No | No | Partial | Yes | LLM reasoning traceability |
| Centralized log shipper | **Yes** | Partial | **Yes** (Loki/CW) | **Yes** | Production observability |
