---
layout: solution
title: "Agent Doesn't Implement Trace-Based Testing"
category: testing
description: "Record execution traces (tool calls, model inputs/outputs, state transitions) during live runs, then assert against the trace structure to verify behavior without re-running expensive model calls."
tags: [testing, tracing, observability, replay, sqlite, python]
---

# Agent Doesn't Implement Trace-Based Testing

Testing agent behavior with full model calls is expensive and non-deterministic. Trace-based testing records what the agent actually did (which tools it called, in what order, with what arguments) and asserts against the trace — combining the realism of integration tests with the speed of unit tests.

## Option 1: In-Memory Trace Collector with Assertions

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TraceEvent:
    event_type: str
    data: dict

@dataclass
class Trace:
    events: list[TraceEvent] = field(default_factory=list)

    def record(self, event_type: str, **data):
        self.events.append(TraceEvent(event_type, data))

    def tool_calls(self) -> list[dict]:
        return [e.data for e in self.events if e.event_type == "tool_call"]

    def model_requests(self) -> list[dict]:
        return [e.data for e in self.events if e.event_type == "model_request"]

    def assert_tool_called(self, tool_name: str):
        names = [tc["name"] for tc in self.tool_calls()]
        assert tool_name in names, f"Expected tool '{tool_name}', got: {names}"

    def assert_tool_call_order(self, *tool_names: str):
        names = [tc["name"] for tc in self.tool_calls()]
        ordered = [n for n in names if n in tool_names]
        assert list(tool_names) == ordered, f"Expected order {list(tool_names)}, got {ordered}"

    def assert_no_tool_called(self, tool_name: str):
        names = [tc["name"] for tc in self.tool_calls()]
        assert tool_name not in names, f"Tool '{tool_name}' should NOT have been called"

    def assert_model_called_once(self):
        assert len(self.model_requests()) == 1, (
            f"Expected 1 model call, got {len(self.model_requests())}"
        )

TOOLS = [
    {"name": "search_web",  "description": "Search the web", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_weather", "description": "Get weather",    "input_schema": {"type": "object", "properties": {"city":  {"type": "string"}}, "required": ["city"]}},
    {"name": "calculate",   "description": "Calculate math", "input_schema": {"type": "object", "properties": {"expr":  {"type": "string"}}, "required": ["expr"]}},
]

def run_agent(user_input: str, trace: Trace) -> str:
    trace.record("model_request", prompt=user_input)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=TOOLS,
        messages=[{"role": "user", "content": user_input}],
    )
    trace.record("model_response", stop_reason=resp.stop_reason)
    if resp.stop_reason == "tool_use":
        for block in resp.content:
            if block.type == "tool_use":
                trace.record("tool_call", name=block.name, inputs=block.input)
    return next((b.text for b in resp.content if hasattr(b, "text")), "")

def test_weather_query_uses_weather_tool():
    trace = Trace()
    run_agent("What's the weather in Tokyo?", trace)
    trace.assert_tool_called("get_weather")
    trace.assert_no_tool_called("calculate")
    trace.assert_model_called_once()
    print("[PASS] weather query uses weather tool")

def test_math_query_uses_calculator():
    trace = Trace()
    run_agent("What is 144 divided by 12?", trace)
    trace.assert_tool_called("calculate")
    trace.assert_no_tool_called("search_web")
    print("[PASS] math query uses calculator")

test_weather_query_uses_weather_tool()
test_math_query_uses_calculator()

# Expected Token Savings: Haiku for trace generation; assertions run locally at zero cost
# Environment: pure Python; Trace is framework-agnostic — works with pytest or standalone
```

## Option 2: SQLite Trace Storage with Replay and Diff

```python
import anthropic
import sqlite3
import json
import time
import uuid
import hashlib

client = anthropic.Anthropic()
DB = "traces.db"

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY, scenario TEXT,
            created_at REAL, input_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT, seq INTEGER,
            event_type TEXT, data TEXT, ts REAL
        );
    """)
    con.commit(); con.close()

class PersistentTracer:
    def __init__(self, scenario: str, user_input: str):
        self.trace_id = uuid.uuid4().hex[:12]
        self.scenario = scenario
        self.seq = 0
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO traces VALUES (?,?,?,?)",
                    (self.trace_id, scenario, time.time(),
                     hashlib.sha256(user_input.encode()).hexdigest()[:16]))
        con.commit(); con.close()

    def record(self, event_type: str, **data):
        self.seq += 1
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO events VALUES (NULL,?,?,?,?,?)",
                    (self.trace_id, self.seq, event_type, json.dumps(data), time.time()))
        con.commit(); con.close()

def load_trace(trace_id: str) -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT seq, event_type, data FROM events WHERE trace_id=? ORDER BY seq",
        (trace_id,)
    ).fetchall()
    con.close()
    return [{"seq": r[0], "type": r[1], **json.loads(r[2])} for r in rows]

def diff_traces(trace_a: list[dict], trace_b: list[dict]) -> list[str]:
    diffs = []
    a_tools = [e for e in trace_a if e["type"] == "tool_call"]
    b_tools = [e for e in trace_b if e["type"] == "tool_call"]
    if len(a_tools) != len(b_tools):
        diffs.append(f"Tool call count: {len(a_tools)} vs {len(b_tools)}")
    for i, (a, b) in enumerate(zip(a_tools, b_tools)):
        if a.get("name") != b.get("name"):
            diffs.append(f"Tool[{i}]: '{a.get('name')}' vs '{b.get('name')}'")
    return diffs

init_db()

TOOLS = [
    {"name": "lookup", "description": "Look up info", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
]

def traced_run(scenario: str, user_input: str) -> tuple[str, str]:
    tracer = PersistentTracer(scenario, user_input)
    tracer.record("model_request", prompt=user_input)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=TOOLS,
        messages=[{"role": "user", "content": user_input}],
    )
    tracer.record("model_response", stop_reason=resp.stop_reason,
                  content_types=[b.type for b in resp.content])
    if resp.stop_reason == "tool_use":
        for b in resp.content:
            if b.type == "tool_use":
                tracer.record("tool_call", name=b.name, inputs=b.input)
    return tracer.trace_id, next((b.text for b in resp.content if hasattr(b, "text")), "")

id1, _ = traced_run("lookup_test", "Look up Python asyncio")
id2, _ = traced_run("lookup_test", "Look up Python asyncio")

trace1 = load_trace(id1)
trace2 = load_trace(id2)
diffs = diff_traces(trace1, trace2)
print(f"Trace diff: {diffs if diffs else 'No structural differences'}")

# Expected Token Savings: Stored traces enable regression testing without re-running model
# Environment: SQLite; load_trace works across sessions; use diff_traces in CI
```

## Option 3: Span-Based Trace with Timing Assertions

```python
import anthropic
import time
import uuid
from dataclasses import dataclass, field
from contextlib import contextmanager

client = anthropic.Anthropic()

@dataclass
class Span:
    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    parent_id: str | None = None
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    tags: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def duration_s(self) -> float:
        return (self.end_time or time.monotonic()) - self.start_time

    def finish(self, error: str | None = None):
        self.end_time = time.monotonic()
        self.error = error

@dataclass
class SpanTrace:
    spans: list[Span] = field(default_factory=list)
    _stack: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **tags):
        parent_id = self._stack[-1].span_id if self._stack else None
        s = Span(name=name, parent_id=parent_id, tags=tags)
        self.spans.append(s)
        self._stack.append(s)
        try:
            yield s
        except Exception as e:
            s.finish(error=str(e)); raise
        finally:
            if not s.end_time: s.finish()
            if self._stack and self._stack[-1] is s: self._stack.pop()

    def assert_span_exists(self, name: str):
        names = [s.name for s in self.spans]
        assert name in names, f"Span '{name}' not found in: {names}"

    def assert_span_duration_lt(self, name: str, max_s: float):
        for s in self.spans:
            if s.name == name:
                assert s.duration_s < max_s, f"Span '{name}' took {s.duration_s:.2f}s > {max_s}s"
                return
        raise AssertionError(f"Span '{name}' not found")

    def assert_no_errors(self):
        errors = [(s.name, s.error) for s in self.spans if s.error]
        assert not errors, f"Trace errors: {errors}"

    def assert_span_order(self, *names: str):
        ordered = [s.name for s in self.spans if s.name in names]
        assert list(names) == ordered, f"Expected {list(names)}, got {ordered}"

    def summary(self) -> str:
        return "\n".join(
            f"{'  ' if s.parent_id else ''}{s.name}: {s.duration_s:.3f}s"
            + (f" [ERR: {s.error}]" if s.error else "")
            for s in self.spans
        )

def run_with_trace(user_input: str, trace: SpanTrace) -> str:
    with trace.span("agent_turn"):
        with trace.span("model_call"):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": user_input}],
            )
        with trace.span("response_parse"):
            result = resp.content[0].text
    return result

def test_timing_slo():
    trace = SpanTrace()
    run_with_trace("What is 2+2?", trace)
    trace.assert_span_exists("model_call")
    trace.assert_span_duration_lt("agent_turn", max_s=30.0)
    trace.assert_no_errors()
    trace.assert_span_order("agent_turn", "model_call", "response_parse")
    print("[PASS] timing SLO test")
    print(trace.summary())

test_timing_slo()

# Expected Token Savings: Span assertions run locally; slow spans flagged without re-running model
# Environment: pure Python; integrate with OpenTelemetry by replacing Span with otel Span
```

## Option 4: Golden Trace Snapshot Testing

```python
import anthropic
import json
from pathlib import Path

client = anthropic.Anthropic()
SNAPSHOT_DIR = Path("trace_snapshots")

def normalize_trace(events: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k not in ("ts", "trace_id", "span_id")} for e in events]

def capture_trace(user_input: str) -> list[dict]:
    TOOLS = [
        {"name": "search", "description": "Search",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    ]
    events = [{"type": "request", "model": "claude-haiku-4-5-20251001"}]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=128, tools=TOOLS,
        messages=[{"role": "user", "content": user_input}],
    )
    events.append({"type": "response", "stop_reason": resp.stop_reason,
                   "content_types": [b.type for b in resp.content]})
    for b in resp.content:
        if b.type == "tool_use":
            events.append({"type": "tool_call", "name": b.name, "inputs": b.input})
    return events

def assert_matches_snapshot(scenario: str, trace: list[dict]) -> bool:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{scenario}.json"
    if not path.exists():
        path.write_text(json.dumps(normalize_trace(trace), indent=2))
        print(f"[NEW SNAPSHOT] {scenario}")
        return True
    expected = json.loads(path.read_text())
    actual = normalize_trace(trace)
    if expected == actual:
        print(f"[PASS] {scenario}: matches snapshot")
        return True
    print(f"[FAIL] {scenario}: trace changed!")
    for i, (e, a) in enumerate(zip(expected, actual)):
        if e != a:
            print(f"  event[{i}] expected: {e}")
            print(f"  event[{i}]   actual: {a}")
    return False

for scenario, query in [("simple_qa", "What is Python?"), ("tool_trigger", "Search for asyncio docs")]:
    trace = capture_trace(query)
    assert_matches_snapshot(scenario, trace)

# Expected Token Savings: After first snapshot, zero model calls needed to re-run assertions
# Environment: commit trace_snapshots/ to version control; update intentionally with path.write_text()
```

## Option 5: Property-Based Trace Assertions

```python
import anthropic
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class TraceProperty:
    name: str
    check: Callable[[list[dict]], bool]
    description: str

@dataclass
class PropertyTrace:
    events: list[dict] = field(default_factory=list)

    def record(self, **event):
        self.events.append(event)

    def verify(self, *properties: TraceProperty) -> bool:
        all_passed = True
        for prop in properties:
            try:
                passed = prop.check(self.events)
                status = "PASS" if passed else "FAIL"
                print(f"  [{status}] {prop.name}: {prop.description}")
                if not passed: all_passed = False
            except Exception as e:
                print(f"  [ERROR] {prop.name}: {e}")
                all_passed = False
        return all_passed

PROPERTIES = [
    TraceProperty("exactly_one_model_call",
        lambda events: sum(1 for e in events if e.get("type") == "model_request") == 1,
        "Agent makes exactly one model call per turn"),
    TraceProperty("no_empty_tool_inputs",
        lambda events: all(e.get("inputs") for e in events if e.get("type") == "tool_call"),
        "All tool calls have non-empty inputs"),
    TraceProperty("response_is_non_empty",
        lambda events: any(
            e.get("type") == "final" and len(e.get("text", "")) > 0 for e in events),
        "Agent produces a non-empty response"),
    TraceProperty("single_request",
        lambda events: sum(1 for e in events if e.get("type") == "model_request") == 1,
        "Exactly one model request per turn"),
]

TOOLS = [
    {"name": "search", "description": "Search web",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
]

def run_with_properties(user_input: str) -> PropertyTrace:
    trace = PropertyTrace()
    trace.record(type="model_request", prompt=user_input[:50])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=128, tools=TOOLS,
        messages=[{"role": "user", "content": user_input}],
    )
    if resp.stop_reason == "tool_use":
        for b in resp.content:
            if b.type == "tool_use":
                trace.record(type="tool_call", name=b.name, inputs=b.input)
    text = next((b.text for b in resp.content if hasattr(b, "text")), "")
    trace.record(type="final", text=text)
    return trace

for p in ["What is 17 * 23?", "Search for asyncio tutorials", "Explain Python"]:
    print(f"\nTesting: {p}")
    trace = run_with_properties(p)
    trace.verify(*PROPERTIES)

# Expected Token Savings: Properties run on recorded trace at zero cost after capture
# Environment: add domain-specific properties per agent type; properties are composable
```

## Option 6: Trace-Driven Regression CI with SQLite History

```python
import anthropic
import sqlite3
import json
import time
import uuid

client = anthropic.Anthropic()
DB = "trace_regression.db"

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS test_runs (
            run_id TEXT PRIMARY KEY, scenario TEXT,
            model TEXT, ts REAL, passed INTEGER, trace TEXT
        );
        CREATE TABLE IF NOT EXISTS assertions (
            run_id TEXT, assertion TEXT, passed INTEGER, message TEXT
        );
    """)
    con.commit(); con.close()

def run_scenario(scenario: str, user_input: str) -> dict:
    run_id = uuid.uuid4().hex[:10]
    TOOLS = [{"name": "lookup", "description": "Lookup info",
              "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}]
    events = [{"type": "request"}]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=128, tools=TOOLS,
        messages=[{"role": "user", "content": user_input}],
    )
    events.append({"type": "response", "stop_reason": resp.stop_reason})
    for b in resp.content:
        if b.type == "tool_use":
            events.append({"type": "tool_call", "name": b.name, "inputs": b.input})
    text = next((b.text for b in resp.content if hasattr(b, "text")), "")
    events.append({"type": "final", "text": text[:200]})
    return {"run_id": run_id, "scenario": scenario, "events": events}

def assert_and_persist(run: dict, assertions: list[tuple]):
    all_passed = all(a[1] for a in assertions)
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO test_runs VALUES (?,?,?,?,?,?)",
                (run["run_id"], run["scenario"], "claude-haiku-4-5-20251001",
                 time.time(), int(all_passed), json.dumps(run["events"])))
    for name, passed, message in assertions:
        con.execute("INSERT INTO assertions VALUES (?,?,?,?)",
                    (run["run_id"], name, int(passed), message))
    con.commit(); con.close()
    return all_passed

def regression_report(scenario: str, last_n: int = 5) -> dict:
    con = sqlite3.connect(DB)
    runs = con.execute(
        "SELECT run_id, ts, passed FROM test_runs WHERE scenario=? ORDER BY ts DESC LIMIT ?",
        (scenario, last_n)
    ).fetchall()
    con.close()
    pass_rate = sum(r[2] for r in runs) / len(runs) * 100 if runs else 0
    return {"scenario": scenario, "total_runs": len(runs), "pass_rate_pct": round(pass_rate, 1)}

init_db()

for scenario, query in [("topic_lookup", "Look up Python coroutines"), ("general_qa", "What is asyncio?")]:
    run = run_scenario(scenario, query)
    events = run["events"]
    assertions = [
        ("has_request",   any(e["type"] == "request" for e in events), "Must have request"),
        ("has_final",     any(e["type"] == "final" for e in events),   "Must have final response"),
        ("non_empty_response", any(
            e["type"] == "final" and len(e.get("text", "")) > 0 for e in events), "Non-empty response"),
    ]
    passed = assert_and_persist(run, assertions)
    print(f"[{'PASS' if passed else 'FAIL'}] {scenario}")
    for name, ok, _ in assertions:
        print(f"  {'v' if ok else 'x'} {name}")

print("\nRegression history:")
for scenario, _ in [("topic_lookup", ""), ("general_qa", "")]:
    r = regression_report(scenario)
    print(f"  {r['scenario']}: {r['pass_rate_pct']}% pass ({r['total_runs']} runs)")

# Expected Token Savings: Historical trace comparison detects regressions at zero model cost
# Environment: SQLite; run in CI; pass_rate_pct tracks test health over time
```

## Comparison

| Option | Trace Storage | Assertion Style | Replay Cost |
|--------|--------------|----------------|-------------|
| 1 — In-Memory Collector | In-memory list | Method assertions | Zero (after capture) |
| 2 — SQLite Persistent | SQLite events | Structural diff | Zero (replay from DB) |
| 3 — Span-Based | In-memory spans | Timing + order | Zero (after capture) |
| 4 — Golden Snapshot | JSON files | Exact match | Zero (snapshot compare) |
| 5 — Property-Based | In-memory list | Predicate functions | Zero (after capture) |
| 6 — Regression CI | SQLite + history | Assertion + pass rate | Zero (historical compare) |
