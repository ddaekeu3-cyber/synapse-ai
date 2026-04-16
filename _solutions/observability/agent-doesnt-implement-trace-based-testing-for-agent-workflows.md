---
title: "Agent Doesn't Implement Trace-Based Testing for Agent Workflows"
description: "AI agents tested only with unit tests miss emergent failures that only appear in full workflow execution: a tool call that succeeds individually but produces output that breaks the next step, a retry that passes but doubles a side effect, or a timeout that gets swallowed silently. Trace-based testing captures the full OTel span tree from a real workflow run and asserts structural properties: span count, step ordering, error spans, latency bounds, and token usage — without requiring a deterministic LLM response."
date: 2025-02-15
difficulty: advanced
category: observability
slug: agent-doesnt-implement-trace-based-testing-for-agent-workflows
tags:
  - trace-based-testing
  - opentelemetry
  - integration-testing
  - span-assertions
  - workflow-testing
  - observability
  - testing
symptoms:
  - "Unit tests pass but production agent fails on multi-step workflows"
  - "No test coverage for span ordering — steps execute out of sequence in one environment"
  - "Token usage regression goes undetected until billing alert fires"
  - "Retry storms appear in traces but no test asserts that retries are bounded"
  - "A timeout that should surface as an error span is silently swallowed — only visible in traces"
---

## Problem

Unit tests validate individual functions in isolation. Agent workflows are more than the sum of their parts: the interaction between steps — data flow, error propagation, retry behaviour, latency — only emerges at runtime. OTel traces capture the complete causal tree of a workflow execution. Trace-based tests run the agent against a real or stubbed environment, capture the resulting span tree in-memory, and assert structural properties: that a specific span exists, that it completes within a time budget, that no error spans are present, that steps execute in the expected order. This catches integration failures that unit tests cannot.

---

## Solution 1: InMemorySpanExporter — Capture Spans During Tests

```python
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    _OTEL = True
except ImportError:
    _OTEL = False


if _OTEL:
    class InMemorySpanExporter(SpanExporter):
        """
        OTel SpanExporter that stores spans in memory for test assertions.

        Usage (in test setup):
            exporter = InMemorySpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(
                SimpleSpanProcessor(exporter)
            )
            trace.set_tracer_provider(provider)

            # Run the agent:
            await agent.run(query="summarise document")

            # Assert on spans:
            spans = exporter.get_finished_spans()
            assert exporter.has_span("llm.invoke")
            assert exporter.span_count() >= 3
        """

        def __init__(self):
            self._spans: List[ReadableSpan] = []
            self._lock = threading.Lock()

        def export(self, spans) -> "SpanExportResult":
            with self._lock:
                self._spans.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def get_finished_spans(self) -> List[ReadableSpan]:
            with self._lock:
                return list(self._spans)

        def clear(self):
            with self._lock:
                self._spans.clear()

        def has_span(self, name_fragment: str) -> bool:
            return any(name_fragment in s.name for s in self.get_finished_spans())

        def get_spans_by_name(self, name_fragment: str) -> List[ReadableSpan]:
            return [s for s in self.get_finished_spans()
                    if name_fragment in s.name]

        def span_count(self) -> int:
            return len(self.get_finished_spans())

        def error_spans(self) -> List[ReadableSpan]:
            from opentelemetry.trace import StatusCode
            return [s for s in self.get_finished_spans()
                    if s.status.status_code == StatusCode.ERROR]
else:
    class InMemorySpanExporter:  # type: ignore
        def __init__(self): self._spans = []
        def get_finished_spans(self): return []
        def clear(self): pass
        def has_span(self, n): return False
        def span_count(self): return 0
        def error_spans(self): return []
```

---

## Solution 2: SpanAssertions — Fluent Assertion API for Span Trees

```python
from typing import Any, Dict, List, Optional


class SpanAssertions:
    """
    Fluent assertion helper for OTel span trees captured by InMemorySpanExporter.
    Provides readable, pytest-friendly assertions for workflow structure.

    Usage:
        sa = SpanAssertions(exporter.get_finished_spans())
        (sa
            .has_span("llm.invoke")
            .has_span("tool.web_search")
            .no_error_spans()
            .span_count_gte(3)
            .attribute_equals("llm.invoke", "gen_ai.request.model", "claude-sonnet-4-6")
            .duration_under("tool.web_search", max_ms=5000)
        )
    """

    def __init__(self, spans):
        self._spans = spans

    def _find(self, name_fragment: str):
        return [s for s in self._spans if name_fragment in s.name]

    def has_span(self, name_fragment: str) -> "SpanAssertions":
        matches = self._find(name_fragment)
        assert matches, (
            f"Expected span containing '{name_fragment}' but found none. "
            f"Available spans: {[s.name for s in self._spans]}"
        )
        return self

    def no_span(self, name_fragment: str) -> "SpanAssertions":
        matches = self._find(name_fragment)
        assert not matches, (
            f"Expected no span containing '{name_fragment}' but found: "
            f"{[s.name for s in matches]}"
        )
        return self

    def no_error_spans(self) -> "SpanAssertions":
        try:
            from opentelemetry.trace import StatusCode
            errors = [s for s in self._spans
                      if s.status.status_code == StatusCode.ERROR]
        except ImportError:
            errors = []
        assert not errors, (
            f"Expected no error spans but found: "
            f"{[(s.name, s.status.description) for s in errors]}"
        )
        return self

    def has_error_span(self, name_fragment: str) -> "SpanAssertions":
        try:
            from opentelemetry.trace import StatusCode
            errors = [s for s in self._find(name_fragment)
                      if s.status.status_code == StatusCode.ERROR]
        except ImportError:
            errors = []
        assert errors, (
            f"Expected error span containing '{name_fragment}' but found none"
        )
        return self

    def span_count_gte(self, n: int) -> "SpanAssertions":
        assert len(self._spans) >= n, (
            f"Expected >= {n} spans but got {len(self._spans)}"
        )
        return self

    def span_count_equals(self, n: int) -> "SpanAssertions":
        assert len(self._spans) == n, (
            f"Expected exactly {n} spans but got {len(self._spans)}"
        )
        return self

    def attribute_equals(self, span_name: str, attr: str,
                          expected: Any) -> "SpanAssertions":
        spans = self._find(span_name)
        assert spans, f"No span containing '{span_name}'"
        for s in spans:
            actual = s.attributes.get(attr) if s.attributes else None
            assert actual == expected, (
                f"Span '{s.name}' attribute '{attr}': "
                f"expected {expected!r}, got {actual!r}"
            )
        return self

    def attribute_present(self, span_name: str, attr: str) -> "SpanAssertions":
        spans = self._find(span_name)
        assert spans, f"No span containing '{span_name}'"
        for s in spans:
            assert s.attributes and attr in s.attributes, (
                f"Span '{s.name}' missing attribute '{attr}'. "
                f"Present attributes: {list(s.attributes or {})}"
            )
        return self

    def duration_under(self, span_name: str, max_ms: float) -> "SpanAssertions":
        spans = self._find(span_name)
        assert spans, f"No span containing '{span_name}'"
        for s in spans:
            duration_ms = (s.end_time - s.start_time) / 1e6  # ns -> ms
            assert duration_ms <= max_ms, (
                f"Span '{s.name}' took {duration_ms:.0f} ms > limit {max_ms} ms"
            )
        return self

    def ordered_before(self, first: str, second: str) -> "SpanAssertions":
        first_spans = self._find(first)
        second_spans = self._find(second)
        assert first_spans, f"No span '{first}'"
        assert second_spans, f"No span '{second}'"
        assert first_spans[0].start_time < second_spans[0].start_time, (
            f"Expected '{first}' to start before '{second}'"
        )
        return self
```

---

## Solution 3: WorkflowTraceFixture — pytest Fixture for Trace Testing

```python
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Callable, Generator, Optional


class WorkflowTraceFixture:
    """
    pytest-compatible fixture that wires up an InMemorySpanExporter,
    runs a workflow, and returns the span tree for assertions.

    Usage in conftest.py:
        @pytest.fixture
        def trace_fixture():
            return WorkflowTraceFixture()

    Usage in tests:
        async def test_summarise_workflow(trace_fixture):
            async with trace_fixture.run() as spans:
                await agent.summarise(doc_url="https://example.com/paper.pdf")

            SpanAssertions(spans).has_span("llm.invoke").no_error_spans()
    """

    def __init__(self):
        self._exporter: Optional[InMemorySpanExporter] = None

    def setup(self):
        if not _OTEL:
            return
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry import trace

        self._exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        trace.set_tracer_provider(provider)

    def teardown(self):
        if self._exporter:
            self._exporter.clear()

    @asynccontextmanager
    async def run(self):
        self.setup()
        if self._exporter:
            self._exporter.clear()
        try:
            yield
        finally:
            pass  # spans are already flushed via SimpleSpanProcessor

    def spans(self):
        if self._exporter:
            return self._exporter.get_finished_spans()
        return []

    def assert_that(self):
        return SpanAssertions(self.spans())
```

---

## Solution 4: TraceSnapshot — Golden File Regression Testing

```python
import json
import os
from typing import Any, Dict, List, Optional


class TraceSnapshot:
    """
    Captures the structure (span names, attributes, ordering) of a trace
    and compares it against a golden file. Detects workflow regressions
    where a new deploy changes which tools are called or in which order.

    Usage:
        snapshot = TraceSnapshot(snapshot_dir="tests/fixtures/traces")

        # First run (or with UPDATE_SNAPSHOTS=1): writes the golden file
        # Subsequent runs: compares against it
        snapshot.assert_matches("summarise_workflow", spans)
    """

    def __init__(self, snapshot_dir: str = "tests/fixtures/traces"):
        self._dir = snapshot_dir
        os.makedirs(snapshot_dir, exist_ok=True)

    def _serialise(self, spans) -> List[Dict[str, Any]]:
        result = []
        for s in sorted(spans, key=lambda x: x.start_time):
            entry: Dict[str, Any] = {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
            }
            try:
                from opentelemetry.trace import StatusCode
                entry["status"] = s.status.status_code.name
            except Exception:
                pass
            result.append(entry)
        return result

    def assert_matches(self, test_name: str, spans,
                        update: bool = False):
        path = os.path.join(self._dir, f"{test_name}.json")
        current = self._serialise(spans)

        if update or os.environ.get("UPDATE_SNAPSHOTS") == "1":
            with open(path, "w") as f:
                json.dump(current, f, indent=2)
            return

        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(current, f, indent=2)
            return

        with open(path) as f:
            golden = json.load(f)

        current_names = [s["name"] for s in current]
        golden_names = [s["name"] for s in golden]
        assert current_names == golden_names, (
            f"Trace structure mismatch for '{test_name}':\n"
            f"Expected spans: {golden_names}\n"
            f"Actual spans:   {current_names}"
        )
```

---

## Solution 5: TokenUsageAssertions — Budget Regression Guards

```python
from typing import Any, Dict, List, Optional


class TokenUsageAssertions:
    """
    Asserts token usage budgets from trace span attributes.
    Prevents token cost regressions where a prompt change quietly
    increases token consumption per request.

    Usage:
        ta = TokenUsageAssertions(spans, cost_per_1k=0.003)
        ta.total_tokens_under(2000)
        ta.cost_under_usd(0.01)
        ta.no_truncated_responses()
    """

    def __init__(self, spans, cost_per_1k: float = 0.003):
        self._spans = spans
        self._cpt = cost_per_1k

    def _token_spans(self):
        return [s for s in self._spans
                if s.attributes and "gen_ai.usage.total_tokens" in s.attributes]

    def total_tokens_under(self, max_tokens: int) -> "TokenUsageAssertions":
        total = sum(
            int(s.attributes.get("gen_ai.usage.total_tokens", 0))
            for s in self._token_spans()
        )
        assert total <= max_tokens, (
            f"Total tokens {total} exceeds budget {max_tokens}"
        )
        return self

    def cost_under_usd(self, max_usd: float) -> "TokenUsageAssertions":
        total = sum(
            float(s.attributes.get("agent.llm.estimated_cost_usd", 0.0))
            for s in self._spans
            if s.attributes and "agent.llm.estimated_cost_usd" in s.attributes
        )
        assert total <= max_usd, (
            f"Estimated cost ${total:.4f} exceeds budget ${max_usd:.4f}"
        )
        return self

    def no_truncated_responses(self) -> "TokenUsageAssertions":
        truncated = [
            s.name for s in self._spans
            if s.attributes and s.attributes.get("agent.llm.truncated") is True
        ]
        assert not truncated, (
            f"Truncated LLM responses detected in spans: {truncated}"
        )
        return self

    def llm_call_count_lte(self, max_calls: int) -> "TokenUsageAssertions":
        count = len(self._token_spans())
        assert count <= max_calls, (
            f"LLM was called {count} times, expected <= {max_calls}"
        )
        return self
```

---

## Solution 6: AgentTestHarness — Full Integration Test Framework

```python
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional


class AgentTestHarness:
    """
    Full test harness: sets up tracing, runs the agent, and provides
    span assertions, token assertions, and snapshot comparison.

    Usage in pytest:
        @pytest.mark.asyncio
        async def test_rag_workflow():
            async with AgentTestHarness.create() as harness:
                result = await agent.answer("What is SSRF?")

                harness.spans.has_span("tool.web_search")
                harness.spans.no_error_spans()
                harness.tokens.total_tokens_under(1500)
                harness.snapshot.assert_matches("rag_workflow")
    """

    def __init__(self, exporter: InMemorySpanExporter,
                  snapshot_dir: str = "tests/fixtures/traces"):
        self._exporter = exporter
        self._snapshot = TraceSnapshot(snapshot_dir)

    @classmethod
    @asynccontextmanager
    async def create(cls, snapshot_dir: str = "tests/fixtures/traces"):
        exporter = InMemorySpanExporter()
        if _OTEL:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            from opentelemetry import trace
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            trace.set_tracer_provider(provider)

        harness = cls(exporter, snapshot_dir)
        try:
            yield harness
        finally:
            pass

    @property
    def spans(self) -> SpanAssertions:
        return SpanAssertions(self._exporter.get_finished_spans())

    @property
    def tokens(self) -> TokenUsageAssertions:
        return TokenUsageAssertions(self._exporter.get_finished_spans())

    @property
    def snapshot(self) -> TraceSnapshot:
        return self._snapshot

    def raw_spans(self):
        return self._exporter.get_finished_spans()

    def print_span_tree(self):
        for s in sorted(self._exporter.get_finished_spans(),
                         key=lambda x: x.start_time):
            dur_ms = (s.end_time - s.start_time) / 1e6
            print(f"  [{dur_ms:6.0f}ms] {s.name}")
```

---

## Comparison

| Approach | Span Capture | Assertions | Token Budget | Snapshot | pytest |
|---|---|---|---|---|---|
| **InMemorySpanExporter** | Yes | No | No | No | No |
| **SpanAssertions** | No | Yes | No | No | No |
| **WorkflowTraceFixture** | Yes | Via SA | No | No | Yes |
| **TraceSnapshot** | No | Structure | No | Yes | No |
| **TokenUsageAssertions** | No | Token/cost | Yes | No | No |
| **AgentTestHarness** | Yes | Yes | Yes | Yes | Yes |

**Key insight**: the minimal viable trace test asserts three things: (1) the expected set of spans is present, (2) no error spans exist, and (3) total token usage is within budget. These three assertions catch the majority of workflow regressions — a missing tool call, a swallowed exception, and a prompt-bloat regression — without requiring deterministic LLM output. Add `TraceSnapshot` golden files once workflows stabilise to catch structural regressions before they reach production.
