---
title: "Agent Doesn't Implement Span Attribute Enrichment for LLM Calls"
description: "AI agents that create bare OpenTelemetry spans for LLM calls record only timing and status — missing model name, token counts, prompt hash, finish reason, and cost. Without these attributes, trace analysis cannot answer which model variant caused a latency spike, which prompt template is driving cost, or why the agent stopped mid-generation. Span attribute enrichment adds structured metadata to every LLM span at the point of invocation."
date: 2025-02-14
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-span-attribute-enrichment-for-llm-calls
tags:
  - opentelemetry
  - spans
  - llm-observability
  - tracing
  - token-counting
  - prompt-tracking
  - otel
  - observability
symptoms:
  - "LLM spans in Jaeger show only duration and HTTP status — no model, tokens, or finish reason"
  - "Cannot determine which model version caused a p99 latency regression from traces alone"
  - "Token cost per request is unknown; billing anomalies require manual log correlation"
  - "Prompt template changes are invisible in traces — no prompt hash or template ID recorded"
  - "Finish reason 'length' (truncated) is not surfaced in spans; truncation goes undetected"
---

## Problem

A bare LLM span records `http.status_code` and duration. That is insufficient for diagnosing agent issues: a latency spike could be caused by a long prompt, a slow model, or a high `max_tokens` setting — indistinguishable without attributes. Span enrichment adds `gen_ai.*` semantic conventions (OpenTelemetry GenAI Working Group standard), token counts, prompt hashes, finish reasons, and cost estimates directly to the span. Every downstream tool — Jaeger, Grafana Tempo, Honeycomb, Datadog — can then filter, group, and alert on these dimensions without log correlation.

---

## Solution 1: LLMSpanAttributes — GenAI Semantic Conventions

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# OpenTelemetry GenAI semantic convention attribute names
# https://opentelemetry.io/docs/specs/semconv/gen-ai/
GEN_AI_SYSTEM             = "gen_ai.system"
GEN_AI_REQUEST_MODEL      = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_RESPONSE_MODEL     = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS  = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_TOTAL_TOKENS  = "gen_ai.usage.total_tokens"

# Extended agent-specific attributes
AGENT_PROMPT_HASH       = "agent.llm.prompt_hash"
AGENT_PROMPT_TEMPLATE   = "agent.llm.prompt_template"
AGENT_CALL_PURPOSE      = "agent.llm.call_purpose"
AGENT_RETRY_ATTEMPT     = "agent.llm.retry_attempt"
AGENT_ESTIMATED_COST_USD = "agent.llm.estimated_cost_usd"


@dataclass
class LLMCallMetadata:
    """Structured metadata extracted from an LLM request/response pair."""
    system: str                        # "openai" | "anthropic" | "google"
    request_model: str
    response_model: Optional[str]
    input_tokens: int
    output_tokens: int
    finish_reasons: List[str]
    temperature: Optional[float]
    max_tokens: Optional[int]
    prompt_hash: str
    prompt_template: Optional[str]
    call_purpose: Optional[str]
    retry_attempt: int = 0
    estimated_cost_usd: Optional[float] = None

    def as_span_attributes(self) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {
            GEN_AI_SYSTEM: self.system,
            GEN_AI_REQUEST_MODEL: self.request_model,
            GEN_AI_USAGE_INPUT_TOKENS: self.input_tokens,
            GEN_AI_USAGE_OUTPUT_TOKENS: self.output_tokens,
            GEN_AI_USAGE_TOTAL_TOKENS: self.input_tokens + self.output_tokens,
            GEN_AI_RESPONSE_FINISH_REASONS: self.finish_reasons,
            AGENT_PROMPT_HASH: self.prompt_hash,
            AGENT_RETRY_ATTEMPT: self.retry_attempt,
        }
        if self.response_model:
            attrs[GEN_AI_RESPONSE_MODEL] = self.response_model
        if self.temperature is not None:
            attrs[GEN_AI_REQUEST_TEMPERATURE] = self.temperature
        if self.max_tokens is not None:
            attrs[GEN_AI_REQUEST_MAX_TOKENS] = self.max_tokens
        if self.prompt_template:
            attrs[AGENT_PROMPT_TEMPLATE] = self.prompt_template
        if self.call_purpose:
            attrs[AGENT_CALL_PURPOSE] = self.call_purpose
        if self.estimated_cost_usd is not None:
            attrs[AGENT_ESTIMATED_COST_USD] = round(self.estimated_cost_usd, 6)
        return attrs


def prompt_hash(messages: List[Dict[str, str]]) -> str:
    """Stable 12-char SHA-256 prefix of the serialised prompt."""
    import json
    raw = json.dumps(messages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
```

---

## Solution 2: EnrichedLLMSpan — OTel Span Wrapper

```python
import contextlib
import time
from typing import Any, Dict, Generator, List, Optional

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    _OTEL = True
except ImportError:
    _OTEL = False


class EnrichedLLMSpan:
    """
    Context manager that creates an OpenTelemetry span for an LLM call
    and enriches it with GenAI semantic convention attributes on exit.

    Usage:
        tracer = trace.get_tracer("my-agent")

        with EnrichedLLMSpan(tracer, "llm.invoke", purpose="summarise") as span_ctx:
            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                max_tokens=512,
            )
            span_ctx.record_response(response)
    """

    def __init__(self, tracer, span_name: str,
                 purpose: Optional[str] = None,
                 template: Optional[str] = None,
                 retry: int = 0):
        self._tracer = tracer
        self._span_name = span_name
        self._purpose = purpose
        self._template = template
        self._retry = retry
        self._span = None
        self._t0 = 0.0

    def __enter__(self):
        if _OTEL:
            self._span = self._tracer.start_span(self._span_name)
            if self._purpose:
                self._span.set_attribute(AGENT_CALL_PURPOSE, self._purpose)
            if self._template:
                self._span.set_attribute(AGENT_PROMPT_TEMPLATE, self._template)
            if self._retry:
                self._span.set_attribute(AGENT_RETRY_ATTEMPT, self._retry)
        self._t0 = time.monotonic()
        return self

    def set_prompt(self, messages: List[Dict[str, str]],
                   model: str, system: str = "openai",
                   temperature: Optional[float] = None,
                   max_tokens: Optional[int] = None):
        if not _OTEL or not self._span:
            return
        self._span.set_attribute(GEN_AI_SYSTEM, system)
        self._span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        self._span.set_attribute(AGENT_PROMPT_HASH, prompt_hash(messages))
        if temperature is not None:
            self._span.set_attribute(GEN_AI_REQUEST_TEMPERATURE, temperature)
        if max_tokens is not None:
            self._span.set_attribute(GEN_AI_REQUEST_MAX_TOKENS, max_tokens)

    def record_response(self, response: Any,
                         cost_per_1k_input: float = 0.0,
                         cost_per_1k_output: float = 0.0):
        """Record OpenAI-compatible response object attributes."""
        if not _OTEL or not self._span:
            return
        usage = getattr(response, "usage", None)
        if usage:
            inp = getattr(usage, "prompt_tokens", 0) or 0
            out = getattr(usage, "completion_tokens", 0) or 0
            self._span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, inp)
            self._span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, out)
            self._span.set_attribute(GEN_AI_USAGE_TOTAL_TOKENS, inp + out)
            if cost_per_1k_input or cost_per_1k_output:
                cost = (inp / 1000 * cost_per_1k_input +
                        out / 1000 * cost_per_1k_output)
                self._span.set_attribute(AGENT_ESTIMATED_COST_USD, round(cost, 6))

        choices = getattr(response, "choices", [])
        reasons = [getattr(c, "finish_reason", "unknown") or "unknown"
                   for c in choices]
        if reasons:
            self._span.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, reasons)

        resp_model = getattr(response, "model", None)
        if resp_model:
            self._span.set_attribute(GEN_AI_RESPONSE_MODEL, resp_model)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not _OTEL or not self._span:
            return False
        if exc_type:
            self._span.set_status(Status(StatusCode.ERROR, str(exc_val)))
            self._span.record_exception(exc_val)
        else:
            self._span.set_status(Status(StatusCode.OK))
        self._span.end()
        return False
```

---

## Solution 3: AnthropicSpanEnricher — Claude-Specific Attributes

```python
from typing import Any, Dict, List, Optional


class AnthropicSpanEnricher:
    """
    Extracts span attributes from Anthropic Claude API responses.
    Handles Claude-specific fields: stop_reason, stop_sequence,
    cache_creation_input_tokens, cache_read_input_tokens.

    Usage:
        enricher = AnthropicSpanEnricher(
            model="claude-sonnet-4-6",
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
        )
        attrs = enricher.extract(response, messages)
        for k, v in attrs.items():
            span.set_attribute(k, v)
    """

    def __init__(self, model: str,
                 cost_per_1k_input: float = 0.003,
                 cost_per_1k_output: float = 0.015,
                 cost_per_1k_cache_write: float = 0.00375,
                 cost_per_1k_cache_read: float = 0.0003):
        self._model = model
        self._cpi = cost_per_1k_input
        self._cpo = cost_per_1k_output
        self._cpcw = cost_per_1k_cache_write
        self._cpcr = cost_per_1k_cache_read

    def extract(self, response: Any,
                messages: Optional[List[Dict]] = None) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {
            GEN_AI_SYSTEM: "anthropic",
            GEN_AI_REQUEST_MODEL: self._model,
        }

        # Usage
        usage = getattr(response, "usage", None)
        if usage:
            inp = getattr(usage, "input_tokens", 0) or 0
            out = getattr(usage, "output_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            attrs[GEN_AI_USAGE_INPUT_TOKENS] = inp
            attrs[GEN_AI_USAGE_OUTPUT_TOKENS] = out
            attrs[GEN_AI_USAGE_TOTAL_TOKENS] = inp + out
            if cache_write:
                attrs["gen_ai.usage.cache_write_tokens"] = cache_write
            if cache_read:
                attrs["gen_ai.usage.cache_read_tokens"] = cache_read
            cost = (inp / 1000 * self._cpi + out / 1000 * self._cpo +
                    cache_write / 1000 * self._cpcw +
                    cache_read / 1000 * self._cpcr)
            attrs[AGENT_ESTIMATED_COST_USD] = round(cost, 6)

        # Stop reason
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason:
            attrs[GEN_AI_RESPONSE_FINISH_REASONS] = [stop_reason]
            if stop_reason == "max_tokens":
                attrs["agent.llm.truncated"] = True

        # Model echo
        resp_model = getattr(response, "model", None)
        if resp_model:
            attrs[GEN_AI_RESPONSE_MODEL] = resp_model

        # Prompt hash
        if messages:
            attrs[AGENT_PROMPT_HASH] = prompt_hash(messages)

        return attrs
```

---

## Solution 4: SpanAttributeMiddleware — Automatic Enrichment for All LLM Calls

```python
import functools
import inspect
from typing import Any, Callable, Optional

try:
    from opentelemetry import trace
    _OTEL = True
except ImportError:
    _OTEL = False


class SpanAttributeMiddleware:
    """
    Decorator/wrapper that automatically enriches OTel spans for any
    async function that calls an LLM. Reads request kwargs and response
    attributes to populate GenAI semantic conventions without requiring
    manual span management at each call site.

    Usage:
        middleware = SpanAttributeMiddleware(
            tracer=trace.get_tracer("agent"),
            system="anthropic",
        )

        @middleware.wrap(purpose="tool-selection", template="tool_selector_v2")
        async def select_tool(messages, model="claude-sonnet-4-6", **kwargs):
            return await anthropic_client.messages.create(
                model=model, messages=messages, **kwargs
            )
    """

    def __init__(self, tracer, system: str = "openai",
                 cost_per_1k_input: float = 0.0,
                 cost_per_1k_output: float = 0.0):
        self._tracer = tracer
        self._system = system
        self._cpi = cost_per_1k_input
        self._cpo = cost_per_1k_output

    def wrap(self, purpose: Optional[str] = None,
             template: Optional[str] = None):
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                if not _OTEL:
                    return await fn(*args, **kwargs)

                span_name = f"llm.{fn.__name__}"
                with self._tracer.start_as_current_span(span_name) as span:
                    # Enrich from call kwargs
                    model = kwargs.get("model", "unknown")
                    messages = kwargs.get("messages", [])
                    span.set_attribute(GEN_AI_SYSTEM, self._system)
                    span.set_attribute(GEN_AI_REQUEST_MODEL, model)
                    span.set_attribute(AGENT_PROMPT_HASH, prompt_hash(messages))
                    if purpose:
                        span.set_attribute(AGENT_CALL_PURPOSE, purpose)
                    if template:
                        span.set_attribute(AGENT_PROMPT_TEMPLATE, template)
                    temp = kwargs.get("temperature")
                    if temp is not None:
                        span.set_attribute(GEN_AI_REQUEST_TEMPERATURE, temp)
                    max_tok = kwargs.get("max_tokens")
                    if max_tok is not None:
                        span.set_attribute(GEN_AI_REQUEST_MAX_TOKENS, max_tok)

                    try:
                        response = await fn(*args, **kwargs)
                    except Exception as exc:
                        from opentelemetry.trace import Status, StatusCode
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        span.record_exception(exc)
                        raise

                    # Enrich from response
                    usage = getattr(response, "usage", None)
                    if usage:
                        inp = getattr(usage, "prompt_tokens",
                                      getattr(usage, "input_tokens", 0)) or 0
                        out = getattr(usage, "completion_tokens",
                                      getattr(usage, "output_tokens", 0)) or 0
                        span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, inp)
                        span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, out)
                        span.set_attribute(GEN_AI_USAGE_TOTAL_TOKENS, inp + out)
                        if self._cpi or self._cpo:
                            cost = (inp / 1000 * self._cpi +
                                    out / 1000 * self._cpo)
                            span.set_attribute(AGENT_ESTIMATED_COST_USD,
                                               round(cost, 6))

                    choices = getattr(response, "choices", [])
                    reasons = [
                        getattr(c, "finish_reason",
                                getattr(response, "stop_reason", "unknown"))
                        for c in choices
                    ] or [getattr(response, "stop_reason", "unknown")]
                    span.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, reasons)
                    return response
            return wrapper
        return decorator
```

---

## Solution 5: TruncationDetector — Alert on Finish Reason `length`

```python
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TruncationDetector:
    """
    Inspects enriched span attributes or raw LLM responses to detect
    truncated generations (finish_reason == 'length' or stop_reason == 'max_tokens').
    Fires an alert callback and records a span event for downstream alerting.

    Usage:
        detector = TruncationDetector(on_truncation=pagerduty_alert)

        response = await llm.invoke(messages, max_tokens=512)
        detector.check(response, context={"agent_id": "a1", "step": "summarise"})
    """

    def __init__(self, on_truncation: Optional[Callable] = None):
        self._callback = on_truncation or self._default_log
        self._truncation_count = 0

    @staticmethod
    def _default_log(ctx: Dict):
        logger.warning("llm_truncated context=%s", ctx)

    def check(self, response: Any, context: Optional[Dict] = None) -> bool:
        """Returns True if generation was truncated."""
        ctx = context or {}
        reasons: List[str] = []

        # OpenAI style
        for c in getattr(response, "choices", []):
            r = getattr(c, "finish_reason", None)
            if r:
                reasons.append(r)

        # Anthropic style
        stop = getattr(response, "stop_reason", None)
        if stop:
            reasons.append(stop)

        truncated = any(r in ("length", "max_tokens") for r in reasons)
        if truncated:
            self._truncation_count += 1
            ctx["finish_reasons"] = reasons
            ctx["truncation_count"] = self._truncation_count
            self._callback(ctx)

            try:
                from opentelemetry import trace
                span = trace.get_current_span()
                span.add_event("llm.truncated", attributes=ctx)
                span.set_attribute("agent.llm.truncated", True)
            except Exception:
                pass

        return truncated

    def stats(self) -> Dict:
        return {"total_truncations": self._truncation_count}
```

---

## Solution 6: LLMObservabilityLayer — Unified Enrichment Stack

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    _OTEL = True
except ImportError:
    _OTEL = False


class LLMObservabilityLayer:
    """
    Unified observability layer combining span enrichment, truncation detection,
    and per-model cost accounting for all LLM calls in an agent.

    Usage:
        obs = LLMObservabilityLayer(
            tracer=trace.get_tracer("my-agent"),
            system="anthropic",
            cost_table={"claude-sonnet-4-6": (0.003, 0.015)},
        )

        response = await obs.call(
            llm_fn=anthropic_client.messages.create,
            messages=messages,
            model="claude-sonnet-4-6",
            max_tokens=1024,
            purpose="plan",
        )
    """

    def __init__(self, tracer,
                 system: str = "anthropic",
                 cost_table: Optional[Dict[str, tuple]] = None):
        self._tracer = tracer
        self._system = system
        self._costs = cost_table or {}
        self._detector = TruncationDetector()

    async def call(self, llm_fn: Callable,
                   messages: List[Dict],
                   model: str,
                   purpose: Optional[str] = None,
                   template: Optional[str] = None,
                   retry: int = 0,
                   **kwargs) -> Any:
        span_name = f"gen_ai.{self._system}.{model}"
        cpi, cpo = self._costs.get(model, (0.0, 0.0))

        if not _OTEL:
            return await llm_fn(messages=messages, model=model, **kwargs)

        with self._tracer.start_as_current_span(span_name) as span:
            span.set_attribute(GEN_AI_SYSTEM, self._system)
            span.set_attribute(GEN_AI_REQUEST_MODEL, model)
            span.set_attribute(AGENT_PROMPT_HASH, prompt_hash(messages))
            span.set_attribute(AGENT_RETRY_ATTEMPT, retry)
            if purpose:
                span.set_attribute(AGENT_CALL_PURPOSE, purpose)
            if template:
                span.set_attribute(AGENT_PROMPT_TEMPLATE, template)
            for k in ("temperature", "max_tokens", "top_p"):
                if k in kwargs:
                    span.set_attribute(f"gen_ai.request.{k}", kwargs[k])

            try:
                response = await llm_fn(
                    messages=messages, model=model, **kwargs
                )
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise

            # Usage
            usage = getattr(response, "usage", None)
            if usage:
                inp = (getattr(usage, "input_tokens", None) or
                       getattr(usage, "prompt_tokens", 0))
                out = (getattr(usage, "output_tokens", None) or
                       getattr(usage, "completion_tokens", 0))
                span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, inp)
                span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, out)
                span.set_attribute(GEN_AI_USAGE_TOTAL_TOKENS, inp + out)
                if cpi or cpo:
                    cost = inp / 1000 * cpi + out / 1000 * cpo
                    span.set_attribute(AGENT_ESTIMATED_COST_USD, round(cost, 6))

            # Finish reasons + truncation
            self._detector.check(response, {"model": model, "purpose": purpose})
            span.set_status(Status(StatusCode.OK))
            return response

    def truncation_stats(self) -> Dict:
        return self._detector.stats()
```

---

## Comparison

| Approach | GenAI Semconv | Token Count | Cost | Finish Reason | Truncation Alert | Auto-Wrap |
|---|---|---|---|---|---|---|
| **LLMSpanAttributes** | Yes | Yes | Yes | Yes | No | No |
| **EnrichedLLMSpan** | Yes | Yes | Yes | Yes | No | No |
| **AnthropicSpanEnricher** | Yes | Yes + cache | Yes | Yes | Partial | No |
| **SpanAttributeMiddleware** | Yes | Yes | Yes | Yes | No | Yes |
| **TruncationDetector** | Partial | No | No | Yes | Yes | No |
| **LLMObservabilityLayer** | Yes | Yes | Yes | Yes | Yes | No |

**Key insight**: record `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, and `agent.llm.prompt_hash` on every LLM span — these four attributes alone unlock cost dashboards, latency-by-prompt-template analysis, and truncation alerting. Use `SpanAttributeMiddleware` as a decorator to enrich all call sites without modifying LLM invocation logic, and add `TruncationDetector` to surface `finish_reason=length` as a span event so on-call engineers see it in Jaeger without log correlation.
