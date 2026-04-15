---
layout: solution
title: "Agent Doesn't Implement Request ID Propagation"
category: general
description: "How to assign and propagate unique request IDs across all agent operations, tool calls, and log entries so that any failure can be traced end-to-end from the original user request."
tags: [general, debugging, tracing, request-id, logging, observability]
---

# Agent Doesn't Implement Request ID Propagation

Without request IDs, debugging a failure means searching logs by time range and hoping you can correlate the right entries. A request ID injected at the boundary and threaded through every log line, tool call, and downstream API call makes any failure immediately traceable — one grep finds every event for that request, across all services and time.

## Option 1: UUID Injection at Request Boundary with Structured Logging

Generate a request ID at the entry point and pass it through all log calls as a structured field.

```python
import anthropic
import uuid
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Optional


# Structured JSON logger
class RequestLogger:
    def __init__(self, service: str = "agent"):
        self.service = service

    def log(self, level: str, request_id: str, event: str, **kwargs):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "service": self.service,
            "request_id": request_id,
            "event": event,
            **kwargs,
        }
        print(json.dumps(entry))

    def info(self, request_id: str, event: str, **kwargs):
        self.log("INFO", request_id, event, **kwargs)

    def error(self, request_id: str, event: str, **kwargs):
        self.log("ERROR", request_id, event, **kwargs)

    def warn(self, request_id: str, event: str, **kwargs):
        self.log("WARN", request_id, event, **kwargs)


logger = RequestLogger(service="agent-service")


def new_request_id() -> str:
    return str(uuid.uuid4())


def call_model(
    client: anthropic.Anthropic,
    request_id: str,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 200,
) -> str:
    logger.info(request_id, "llm.request.start", model=model, prompt_length=len(prompt))
    start = time.monotonic()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        output = response.content[0].text

        logger.info(request_id, "llm.request.complete",
                    model=model,
                    latency_ms=round(latency_ms),
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens)
        return output

    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        logger.error(request_id, "llm.request.failed",
                     model=model,
                     latency_ms=round(latency_ms),
                     error=str(e))
        raise


def execute_tool(request_id: str, tool_name: str, args: dict) -> str:
    logger.info(request_id, "tool.call.start", tool=tool_name, args=args)
    start = time.monotonic()

    try:
        # Simulate tool execution
        result = f"[{tool_name} result for args={args}]"
        latency_ms = (time.monotonic() - start) * 1000
        logger.info(request_id, "tool.call.complete",
                    tool=tool_name,
                    latency_ms=round(latency_ms),
                    result_length=len(result))
        return result
    except Exception as e:
        logger.error(request_id, "tool.call.failed", tool=tool_name, error=str(e))
        raise


def handle_user_request(user_message: str) -> str:
    """Entry point — generate request ID here and thread it everywhere."""
    request_id = new_request_id()
    logger.info(request_id, "request.received", message_length=len(user_message))

    client = anthropic.Anthropic()

    try:
        # Step 1: LLM call
        plan = call_model(client, request_id, f"Plan how to answer: {user_message}")

        # Step 2: Tool call
        result = execute_tool(request_id, "web_search", {"query": user_message[:50]})

        # Step 3: Final synthesis
        answer = call_model(client, request_id,
                            f"Answer: {user_message}\nContext: {result}\nPlan: {plan[:100]}")

        logger.info(request_id, "request.complete", output_length=len(answer))
        return answer

    except Exception as e:
        logger.error(request_id, "request.failed", error=str(e))
        return f"Error: {e}"


if __name__ == "__main__":
    result = handle_user_request("What are the best practices for API rate limiting?")
    print(f"\nResult: {result[:150]}")

# Expected Token Savings: Faster debugging means fewer exploratory re-runs; incidents resolved in minutes not hours
# Environment: Any production agent — this is table-stakes observability infrastructure
```

## Option 2: Context-Variable Request ID — Thread Through Call Stack Automatically

Use a context variable so the request ID is automatically available throughout the call stack without explicit passing.

```python
import anthropic
import uuid
import time
import json
from contextvars import ContextVar
from functools import wraps
from typing import Callable, Optional


# Context variable — automatically scoped to each async task / thread
_request_id: ContextVar[str] = ContextVar("request_id", default="no-request-id")
_parent_span: ContextVar[Optional[str]] = ContextVar("parent_span", default=None)


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def log(level: str, event: str, **kwargs):
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "request_id": get_request_id(),
        "event": event,
        **kwargs,
    }
    print(json.dumps(entry))


def with_request_id(func: Callable) -> Callable:
    """Decorator: auto-generate request ID if not set."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        rid = kwargs.pop("request_id", None) or new_request_id()
        token = _request_id.set(rid)
        try:
            log("INFO", f"{func.__name__}.start")
            result = func(*args, **kwargs)
            log("INFO", f"{func.__name__}.complete")
            return result
        except Exception as e:
            log("ERROR", f"{func.__name__}.failed", error=str(e))
            raise
        finally:
            _request_id.reset(token)
    return wrapper


def new_request_id() -> str:
    return str(uuid.uuid4())[:8]


def llm_call(prompt: str, max_tokens: int = 200) -> str:
    """Uses request_id from context automatically."""
    client = anthropic.Anthropic()
    log("INFO", "llm.call", prompt_len=len(prompt))
    start = time.monotonic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    output = response.content[0].text
    log("INFO", "llm.response",
        latency_ms=round((time.monotonic() - start) * 1000),
        tokens=response.usage.input_tokens + response.usage.output_tokens)
    return output


def tool_call(tool_name: str, args: dict) -> str:
    """Request ID flows from context — no explicit passing needed."""
    log("INFO", "tool.call", tool=tool_name, args=str(args)[:60])
    result = f"[{tool_name} result]"
    log("INFO", "tool.result", tool=tool_name, result_len=len(result))
    return result


def validate_input(text: str) -> bool:
    log("INFO", "validation.check", input_len=len(text))
    valid = len(text.strip()) > 0
    log("INFO", "validation.result", valid=valid)
    return valid


@with_request_id
def process_query(user_query: str) -> str:
    """All calls inside here automatically use the same request_id."""
    if not validate_input(user_query):
        raise ValueError("Empty query")

    plan = llm_call(f"Plan how to answer: {user_query}")
    context = tool_call("knowledge_base", {"query": user_query})
    answer = llm_call(f"Answer '{user_query}' using context: {context[:100]}")

    return answer


if __name__ == "__main__":
    # Each call gets its own isolated request_id
    for query in [
        "How does DNS work?",
        "What is a race condition?",
    ]:
        print(f"\n--- Processing: {query} ---")
        result = process_query(query)
        print(f"Result: {result[:100]}")

# Expected Token Savings: Zero overhead — context vars are O(1); eliminates debugging time across multi-layer call stacks
# Environment: Web servers (FastAPI/Flask), async agents, any agent with multiple layers of helper functions
```

## Option 3: Request ID Header Propagation to External APIs

When the agent calls external APIs or webhooks, propagate the request ID as a header so failures are traceable end-to-end.

```python
import anthropic
import uuid
import time
import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional


REQUEST_ID_HEADER = "X-Request-ID"
UPSTREAM_HEADER = "X-Upstream-Request-ID"


@dataclass
class PropagatedRequest:
    request_id: str
    upstream_id: Optional[str]     # ID from the caller if we're a downstream service
    headers: dict

    @staticmethod
    def new(upstream_id: Optional[str] = None) -> "PropagatedRequest":
        request_id = str(uuid.uuid4())
        headers = {
            REQUEST_ID_HEADER: request_id,
        }
        if upstream_id:
            headers[UPSTREAM_HEADER] = upstream_id
        return PropagatedRequest(
            request_id=request_id,
            upstream_id=upstream_id,
            headers=headers,
        )


def log(request_id: str, event: str, **kwargs):
    print(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": request_id,
        "event": event,
        **{k: str(v)[:100] for k, v in kwargs.items()},
    }))


def call_external_api(
    req: PropagatedRequest,
    url: str,
    method: str = "GET",
    payload: Optional[dict] = None,
) -> dict:
    """Make HTTP call with request ID propagated as header."""
    headers = {
        "Content-Type": "application/json",
        **req.headers,  # Inject X-Request-ID and X-Upstream-Request-ID
    }

    log(req.request_id, "http.request.start", url=url, method=method)
    start = time.monotonic()

    try:
        data = json.dumps(payload).encode() if payload else None
        http_req = urllib.request.Request(url, data=data, headers=headers, method=method)

        with urllib.request.urlopen(http_req, timeout=10) as response:
            body = response.read().decode()
            latency = (time.monotonic() - start) * 1000
            log(req.request_id, "http.request.complete",
                url=url, status=response.status, latency_ms=round(latency))
            return json.loads(body) if body else {}

    except urllib.error.HTTPError as e:
        latency = (time.monotonic() - start) * 1000
        log(req.request_id, "http.request.error",
            url=url, status=e.code, latency_ms=round(latency), error=str(e))
        raise
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        log(req.request_id, "http.request.failed",
            url=url, latency_ms=round(latency), error=str(e))
        raise


def call_llm_with_tracking(
    req: PropagatedRequest,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    client = anthropic.Anthropic()
    log(req.request_id, "llm.request", model=model)
    start = time.monotonic()

    # Pass request ID as user metadata (visible in Anthropic dashboard)
    response = client.messages.create(
        model=model,
        max_tokens=200,
        metadata={"user_id": req.request_id},  # Traceable in Anthropic usage dashboard
        messages=[{"role": "user", "content": prompt}],
    )
    latency = (time.monotonic() - start) * 1000
    log(req.request_id, "llm.response",
        latency_ms=round(latency),
        tokens=response.usage.input_tokens + response.usage.output_tokens)
    return response.content[0].text


def handle_request(user_query: str, upstream_request_id: Optional[str] = None) -> str:
    req = PropagatedRequest.new(upstream_id=upstream_request_id)
    log(req.request_id, "request.received",
        upstream_id=upstream_request_id or "none",
        query_len=len(user_query))

    try:
        answer = call_llm_with_tracking(req, f"Answer: {user_query}")
        log(req.request_id, "request.complete")
        return answer
    except Exception as e:
        log(req.request_id, "request.failed", error=str(e))
        raise


if __name__ == "__main__":
    # Simulate a request coming from an upstream service with its own ID
    upstream_id = "upstream-" + str(uuid.uuid4())[:8]
    print(f"Upstream request ID: {upstream_id}\n")

    result = handle_request("What is HTTP/2?", upstream_request_id=upstream_id)
    print(f"\nResult: {result[:150]}")

# Expected Token Savings: End-to-end traceability reduces mean-time-to-resolution; prevents expensive exploratory debugging
# Environment: Microservice architectures, agents calling external APIs, multi-service agent pipelines
```

## Option 4: Request ID as SQLite Correlation Key for Async Jobs

For async/queued agent jobs, store request IDs in SQLite so any worker that picks up the job continues with the same ID.

```python
import anthropic
import sqlite3
import uuid
import time
import json
from dataclasses import dataclass
from typing import Optional


DB_PATH = ":memory:"


def init_job_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            input TEXT,
            output TEXT,
            created_at REAL,
            started_at REAL,
            completed_at REAL,
            worker_id TEXT,
            error TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            event TEXT NOT NULL,
            data TEXT,
            ts REAL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_req ON job_events(request_id)")
    db.commit()
    return db


def enqueue_job(db: sqlite3.Connection, input_text: str) -> tuple[str, str]:
    """Returns (job_id, request_id)."""
    job_id = str(uuid.uuid4())[:8]
    request_id = str(uuid.uuid4())[:8]
    now = time.time()

    db.execute("""
        INSERT INTO jobs (job_id, request_id, status, input, created_at)
        VALUES (?, ?, 'queued', ?, ?)
    """, (job_id, request_id, input_text, now))
    db.commit()

    log_event(db, request_id, job_id, "job.queued", {"input_len": len(input_text)})
    return job_id, request_id


def claim_job(db: sqlite3.Connection, worker_id: str) -> Optional[tuple[str, str, str]]:
    """Atomically claim next queued job. Returns (job_id, request_id, input) or None."""
    row = db.execute("""
        SELECT job_id, request_id, input FROM jobs
        WHERE status = 'queued'
        ORDER BY created_at ASC LIMIT 1
    """).fetchone()

    if not row:
        return None

    job_id, request_id, input_text = row
    db.execute("""
        UPDATE jobs SET status='running', worker_id=?, started_at=?
        WHERE job_id=? AND status='queued'
    """, (worker_id, time.time(), job_id))
    db.commit()

    log_event(db, request_id, job_id, "job.started", {"worker": worker_id})
    return job_id, request_id, input_text


def complete_job(db: sqlite3.Connection, job_id: str, request_id: str, output: str):
    db.execute("""
        UPDATE jobs SET status='complete', output=?, completed_at=?
        WHERE job_id=?
    """, (output, time.time(), job_id))
    db.commit()
    log_event(db, request_id, job_id, "job.complete", {"output_len": len(output)})


def fail_job(db: sqlite3.Connection, job_id: str, request_id: str, error: str):
    db.execute("""
        UPDATE jobs SET status='failed', error=?, completed_at=?
        WHERE job_id=?
    """, (error, time.time(), job_id))
    db.commit()
    log_event(db, request_id, job_id, "job.failed", {"error": error})


def log_event(db: sqlite3.Connection, request_id: str, job_id: str, event: str, data: dict = None):
    db.execute("""
        INSERT INTO job_events (request_id, job_id, event, data, ts)
        VALUES (?, ?, ?, ?, ?)
    """, (request_id, job_id, event, json.dumps(data or {}), time.time()))
    db.commit()
    print(json.dumps({"request_id": request_id, "job_id": job_id, "event": event, **(data or {})}))


def worker_process(db: sqlite3.Connection, worker_id: str):
    """Worker that picks up jobs and maintains request ID continuity."""
    client = anthropic.Anthropic()
    job = claim_job(db, worker_id)

    if not job:
        return

    job_id, request_id, input_text = job
    log_event(db, request_id, job_id, "llm.call.start", {"worker": worker_id})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            metadata={"user_id": request_id},
            messages=[{"role": "user", "content": input_text}],
        )
        output = response.content[0].text
        log_event(db, request_id, job_id, "llm.call.complete",
                  {"tokens": response.usage.input_tokens + response.usage.output_tokens})
        complete_job(db, job_id, request_id, output)
    except Exception as e:
        fail_job(db, job_id, request_id, str(e))


def get_job_trace(db: sqlite3.Connection, request_id: str) -> list[dict]:
    rows = db.execute("""
        SELECT event, data, ts FROM job_events
        WHERE request_id=? ORDER BY ts
    """, (request_id,)).fetchall()
    return [{"event": r[0], "data": json.loads(r[1]), "ts": r[2]} for r in rows]


if __name__ == "__main__":
    db = init_job_db()

    # Enqueue 3 jobs
    jobs = []
    for query in ["What is Redis?", "Explain gRPC.", "What is idempotency?"]:
        job_id, request_id = enqueue_job(db, query)
        jobs.append((job_id, request_id, query))
        print(f"Enqueued: job={job_id} request={request_id}")

    # Process jobs with worker
    for i in range(3):
        worker_process(db, f"worker-{i+1}")

    # Show full trace for first job
    _, first_request_id, _ = jobs[0]
    print(f"\n=== Full trace for request {first_request_id} ===")
    for event in get_job_trace(db, first_request_id):
        print(f"  {event['event']}: {event['data']}")

# Expected Token Savings: No direct savings — enables fast debugging that prevents expensive re-runs after failures
# Environment: Async job queues, task workers, distributed agent pipelines with multiple workers
```

## Option 5: Request ID in Multi-Turn Conversations — Correlate Entire Session

Maintain a session-level ID and per-turn request IDs so the full conversation history is traceable.

```python
import anthropic
import uuid
import time
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ConversationSession:
    session_id: str
    user_id: Optional[str]
    turns: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def new_turn(self) -> str:
        turn_id = f"{self.session_id}-T{len(self.turns)+1}"
        self.turns.append({"turn_id": turn_id, "ts": time.time()})
        return turn_id


def log(session_id: str, turn_id: str, event: str, **kwargs):
    print(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "turn_id": turn_id,
        "event": event,
        **{k: str(v)[:80] for k, v in kwargs.items()},
    }))


class TracedConversationAgent:
    def __init__(self, user_id: Optional[str] = None):
        self.session = ConversationSession(
            session_id=str(uuid.uuid4())[:8],
            user_id=user_id,
        )
        self.messages = []
        self.client = anthropic.Anthropic()
        log(self.session.session_id, "session-init", "session.started", user_id=user_id or "anonymous")

    def chat(self, user_message: str) -> str:
        turn_id = self.session.new_turn()
        log(self.session.session_id, turn_id, "turn.received",
            message_len=len(user_message), turn_number=len(self.session.turns))

        self.messages.append({"role": "user", "content": user_message})
        start = time.monotonic()

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                metadata={"user_id": f"{self.session.session_id}/{turn_id}"},
                messages=self.messages,
            )
            output = response.content[0].text
            latency = (time.monotonic() - start) * 1000

            self.messages.append({"role": "assistant", "content": output})

            log(self.session.session_id, turn_id, "turn.complete",
                latency_ms=round(latency),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                stop_reason=response.stop_reason)

            return output

        except Exception as e:
            log(self.session.session_id, turn_id, "turn.failed", error=str(e))
            raise

    def end_session(self):
        total_turns = len(self.session.turns)
        duration = time.time() - self.session.created_at
        log(self.session.session_id, "session-end", "session.ended",
            total_turns=total_turns, duration_seconds=round(duration))


if __name__ == "__main__":
    agent = TracedConversationAgent(user_id="user-demo-42")

    conversation = [
        "What is the difference between a process and a thread?",
        "Which one uses more memory?",
        "Give me a Python example showing threads sharing state.",
    ]

    for msg in conversation:
        print(f"\nUser: {msg}")
        response = agent.chat(msg)
        print(f"Agent: {response[:100]}...")

    agent.end_session()

# Expected Token Savings: Session tracing exposes where conversations go long, enabling targeted context trimming
# Environment: Multi-turn chatbots, customer support agents, any conversational agent with persistent sessions
```

## Option 6: Request ID Dashboard — Aggregate Metrics by Request Pattern

Accumulate request IDs and their outcomes into a metrics store for dashboarding.

```python
import anthropic
import uuid
import time
import json
import sqlite3
import statistics
from dataclasses import dataclass
from typing import Optional


@dataclass
class RequestMetrics:
    request_id: str
    endpoint: str
    model: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    success: bool
    error: Optional[str] = None


class MetricsDashboard:
    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS request_metrics (
                request_id TEXT PRIMARY KEY,
                endpoint TEXT,
                model TEXT,
                latency_ms REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                success INTEGER,
                error TEXT,
                ts REAL
            )
        """)
        self.db.commit()

    def record(self, m: RequestMetrics):
        self.db.execute("""
            INSERT OR REPLACE INTO request_metrics
            (request_id, endpoint, model, latency_ms, input_tokens, output_tokens, success, error, ts)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (m.request_id, m.endpoint, m.model, m.latency_ms,
              m.input_tokens, m.output_tokens, int(m.success), m.error, time.time()))
        self.db.commit()

    def report(self):
        rows = self.db.execute("""
            SELECT endpoint, model, COUNT(*),
                   AVG(latency_ms), MAX(latency_ms),
                   SUM(input_tokens + output_tokens),
                   SUM(CASE WHEN success=0 THEN 1 ELSE 0 END)
            FROM request_metrics
            GROUP BY endpoint, model
        """).fetchall()

        print(f"\n{'Endpoint':<25} {'Model':<10} {'Calls':>6} {'P50ms':>8} {'MaxMs':>8} {'Tokens':>10} {'Errors':>8}")
        print("-" * 80)
        for row in rows:
            endpoint, model, calls, avg_ms, max_ms, tokens, errors = row
            print(f"{endpoint:<25} {model.split('-')[1]:<10} {calls:>6} {avg_ms:>8.0f} {max_ms:>8.0f} {tokens:>10} {errors:>8}")

    def failed_requests(self, limit: int = 5) -> list[dict]:
        rows = self.db.execute("""
            SELECT request_id, endpoint, error, ts FROM request_metrics
            WHERE success=0 ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
        return [{"request_id": r[0], "endpoint": r[1], "error": r[2]} for r in rows]


dashboard = MetricsDashboard()


def tracked_llm_call(
    endpoint: str,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    request_id: Optional[str] = None,
) -> tuple[str, str]:
    """Returns (output, request_id)."""
    client = anthropic.Anthropic()
    rid = request_id or str(uuid.uuid4())[:8]
    start = time.monotonic()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = (time.monotonic() - start) * 1000
        output = response.content[0].text

        dashboard.record(RequestMetrics(
            request_id=rid,
            endpoint=endpoint,
            model=model,
            latency_ms=latency,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            success=True,
        ))
        return output, rid

    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        dashboard.record(RequestMetrics(
            request_id=rid,
            endpoint=endpoint,
            model=model,
            latency_ms=latency,
            input_tokens=0,
            output_tokens=0,
            success=False,
            error=str(e),
        ))
        raise


if __name__ == "__main__":
    queries = [
        ("qa-endpoint", "What is TCP?"),
        ("qa-endpoint", "What is UDP?"),
        ("summarize-endpoint", "Summarize: TCP and UDP are transport protocols."),
        ("qa-endpoint", "What is HTTP?"),
        ("code-endpoint", "Write hello world in Python."),
    ]

    print("Processing requests...")
    for endpoint, prompt in queries:
        result, rid = tracked_llm_call(endpoint, prompt)
        print(f"[{rid}] {endpoint}: {result[:50]}...")

    dashboard.report()

    failed = dashboard.failed_requests()
    if failed:
        print(f"\nFailed requests: {failed}")
    else:
        print("\nNo failures recorded.")

# Expected Token Savings: Metrics reveal high-latency endpoints to optimize and error patterns to fix proactively
# Environment: Production APIs, monitoring dashboards, SRE tooling for agent reliability
```

## Comparison

| Option | ID Scope | Storage | Cross-Service | Best For |
|--------|----------|---------|---------------|----------|
| 1 Structured Logging | Per-request | Stdout/logs | No | Any agent as baseline observability |
| 2 Context Variable | Per-request/task | In-memory | No | Multi-layer async agents, FastAPI handlers |
| 3 Header Propagation | Per-request | HTTP headers | Yes | Microservice pipelines, external API calls |
| 4 SQLite Async Jobs | Per-job | SQLite | Worker-portable | Async job queues, distributed workers |
| 5 Session + Turn IDs | Per-session + per-turn | Stdout/logs | No | Multi-turn conversational agents |
| 6 Metrics Dashboard | Per-endpoint aggregate | SQLite | No | Production monitoring, SRE tooling |
