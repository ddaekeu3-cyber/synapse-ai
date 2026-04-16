---
title: "Agent Doesn't Implement Transient Fault Classification"
description: "AI agents retry every error identically — they retry fatal errors (bad API keys, malformed prompts) that will never succeed, and fail to retry transient ones (timeouts, 503s) with the right policy."
category: reliability
difficulty: intermediate
tags: [errors, classification, retry, fault-tolerance, resilience, exceptions, circuit-breaker]
---

# Agent Doesn't Implement Transient Fault Classification

## Problem

Retrying a bad API key wastes budget and delays the user. Not retrying a transient 503 loses a request that would have succeeded 200ms later. Agents need a classification layer that categorizes every error into: **retryable** (transient, worth retrying), **fatal** (will never succeed without external fix), or **degradable** (partial success is better than total failure). Without this taxonomy, retry policies are either too aggressive (hammering a broken auth endpoint) or too timid (giving up on a flaky network call).

## Solution 1: Error Taxonomy with Classified Exception Hierarchy

Define a structured exception hierarchy that encodes retry semantics into the error type itself.

```python
import asyncio
from enum import Enum
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic, APIStatusError, APITimeoutError, APIConnectionError

client = AsyncAnthropic()

class FaultClass(Enum):
    TRANSIENT   = "transient"    # retry with backoff
    FATAL       = "fatal"        # do not retry; surface to caller
    DEGRADABLE  = "degradable"   # partial result acceptable
    QUOTA       = "quota"        # retry after delay dictated by server
    UNKNOWN     = "unknown"      # classify cautiously as transient

@dataclass
class ClassifiedFault(Exception):
    fault_class: FaultClass
    original: Exception
    retry_after: float | None = None  # seconds, for QUOTA faults
    message: str = ""

    def __str__(self):
        return f"[{self.fault_class.value}] {self.message or str(self.original)}"

def classify_anthropic_error(exc: Exception) -> ClassifiedFault:
    """
    Map Anthropic SDK exceptions to fault classes.
    """
    if isinstance(exc, APITimeoutError):
        return ClassifiedFault(
            fault_class=FaultClass.TRANSIENT,
            original=exc,
            message="Request timed out — transient, safe to retry",
        )

    if isinstance(exc, APIConnectionError):
        return ClassifiedFault(
            fault_class=FaultClass.TRANSIENT,
            original=exc,
            message="Network connectivity issue",
        )

    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if status == 429:
            retry_after = float(exc.response.headers.get("retry-after", 5))
            return ClassifiedFault(
                fault_class=FaultClass.QUOTA,
                original=exc,
                retry_after=retry_after,
                message=f"Rate limited; retry after {retry_after}s",
            )
        if status in (500, 502, 503, 504):
            return ClassifiedFault(
                fault_class=FaultClass.TRANSIENT,
                original=exc,
                message=f"Server error {status} — transient",
            )
        if status in (400, 422):
            return ClassifiedFault(
                fault_class=FaultClass.FATAL,
                original=exc,
                message=f"Bad request {status} — fix prompt/parameters",
            )
        if status == 401:
            return ClassifiedFault(
                fault_class=FaultClass.FATAL,
                original=exc,
                message="Authentication failed — invalid API key",
            )
        if status == 403:
            return ClassifiedFault(
                fault_class=FaultClass.FATAL,
                original=exc,
                message="Authorization denied — check permissions",
            )

    return ClassifiedFault(
        fault_class=FaultClass.UNKNOWN,
        original=exc,
        message=str(exc),
    )

async def classified_api_call(prompt: str, max_retries: int = 3) -> str:
    """
    API call with classification-aware retry policy.
    """
    attempt = 0
    delay = 1.0

    while True:
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text

        except Exception as exc:
            fault = classify_anthropic_error(exc)
            attempt += 1

            if fault.fault_class == FaultClass.FATAL:
                raise fault  # never retry

            if fault.fault_class == FaultClass.QUOTA:
                wait = fault.retry_after or delay
                await asyncio.sleep(wait)
                delay = wait  # don't compound backoff on quota errors
                continue

            if fault.fault_class in (FaultClass.TRANSIENT, FaultClass.UNKNOWN):
                if attempt >= max_retries:
                    raise fault
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue

            raise fault  # DEGRADABLE — caller handles
```

**When to use**: Every agent that makes external API calls. This is the minimum required scaffolding for correct retry behavior.

---

## Solution 2: Fault Registry — Centralized Classification Table

Maintain a centralized registry that maps error patterns to fault classes, making it easy to update without touching retry logic.

```python
import re
from dataclasses import dataclass
from typing import Callable
from anthropic import AsyncAnthropic, APIStatusError, APITimeoutError

client = AsyncAnthropic()

@dataclass
class FaultRule:
    name: str
    matcher: Callable[[Exception], bool]
    fault_class: str          # "transient" | "fatal" | "quota" | "degradable"
    base_delay: float = 1.0
    max_delay: float = 30.0
    max_retries: int = 3

FAULT_REGISTRY: list[FaultRule] = [
    FaultRule(
        name="timeout",
        matcher=lambda e: isinstance(e, APITimeoutError),
        fault_class="transient",
        base_delay=2.0,
        max_retries=5,
    ),
    FaultRule(
        name="rate_limit",
        matcher=lambda e: isinstance(e, APIStatusError) and e.status_code == 429,
        fault_class="quota",
        base_delay=5.0,
        max_retries=10,
    ),
    FaultRule(
        name="server_error",
        matcher=lambda e: isinstance(e, APIStatusError) and e.status_code >= 500,
        fault_class="transient",
        base_delay=1.0,
        max_retries=3,
    ),
    FaultRule(
        name="bad_request",
        matcher=lambda e: isinstance(e, APIStatusError) and e.status_code in (400, 422),
        fault_class="fatal",
        max_retries=0,
    ),
    FaultRule(
        name="auth_failure",
        matcher=lambda e: isinstance(e, APIStatusError) and e.status_code in (401, 403),
        fault_class="fatal",
        max_retries=0,
    ),
    FaultRule(
        name="context_length",
        matcher=lambda e: isinstance(e, APIStatusError) and "context" in str(e).lower(),
        fault_class="degradable",
        max_retries=0,
    ),
]

def lookup_fault(exc: Exception) -> FaultRule | None:
    for rule in FAULT_REGISTRY:
        if rule.matcher(exc):
            return rule
    return None

async def resilient_call(prompt: str) -> dict:
    import asyncio
    attempt = 0
    delay = 1.0

    while True:
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return {"status": "ok", "text": resp.content[0].text, "attempts": attempt + 1}

        except Exception as exc:
            rule = lookup_fault(exc)

            if rule is None:
                # Unknown — treat as transient with limited retries
                if attempt >= 2:
                    return {"status": "error", "error": str(exc), "attempts": attempt + 1}
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
                attempt += 1
                continue

            if rule.fault_class == "fatal":
                return {"status": "fatal", "error": rule.name, "exc": str(exc)}

            if rule.fault_class == "degradable":
                return {"status": "degraded", "error": rule.name, "partial": None}

            attempt += 1
            if attempt >= rule.max_retries:
                return {"status": "exhausted", "error": rule.name, "attempts": attempt}

            wait = min(rule.base_delay * (2 ** attempt), rule.max_delay)
            await asyncio.sleep(wait)
```

**When to use**: Teams that need to update fault handling rules without touching retry infrastructure. The registry makes classification auditable and testable independently.

---

## Solution 3: Fault-Aware Circuit Breaker — Track Error Classes Separately

Open the circuit only on transient faults; fatal faults are surfaced immediately without tripping the breaker.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic, APIStatusError, APITimeoutError

client = AsyncAnthropic()

@dataclass
class FaultClassCircuitBreaker:
    """
    Circuit breaker that tracks transient vs fatal faults separately.
    Only transient faults count toward opening the circuit.
    """
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    window_seconds: float = 60.0

    _state: str = field(default="closed", init=False)
    _transient_times: deque = field(default_factory=deque, init=False)
    _open_since: float | None = field(default=None, init=False)
    _total_fatal: int = field(default=0, init=False)
    _total_transient: int = field(default=0, init=False)

    def _prune_window(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._transient_times and self._transient_times[0] < cutoff:
            self._transient_times.popleft()

    def record_transient_failure(self):
        self._total_transient += 1
        now = time.monotonic()
        self._transient_times.append(now)
        self._prune_window()
        if len(self._transient_times) >= self.failure_threshold:
            self._state = "open"
            self._open_since = now

    def record_fatal_failure(self):
        # Fatal errors do NOT trip the circuit
        self._total_fatal += 1

    def record_success(self):
        self._prune_window()
        if self._state == "half-open":
            self._state = "closed"
            self._transient_times.clear()

    def can_attempt(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.monotonic() - (self._open_since or 0) >= self.recovery_timeout:
                self._state = "half-open"
                return True
            return False
        return True  # half-open: allow one probe

    @property
    def stats(self) -> dict:
        self._prune_window()
        return {
            "state": self._state,
            "recent_transient_faults": len(self._transient_times),
            "total_transient": self._total_transient,
            "total_fatal": self._total_fatal,
        }

breaker = FaultClassCircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

def is_transient(exc: Exception) -> bool:
    if isinstance(exc, APITimeoutError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (429, 500, 502, 503, 504):
        return True
    return False

async def circuit_protected_call(prompt: str) -> dict:
    if not breaker.can_attempt():
        return {"error": "circuit_open", "stats": breaker.stats}

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        breaker.record_success()
        return {"text": resp.content[0].text}

    except Exception as exc:
        if is_transient(exc):
            breaker.record_transient_failure()
        else:
            breaker.record_fatal_failure()
        return {"error": str(exc), "fault_type": "transient" if is_transient(exc) else "fatal"}
```

**When to use**: Agents under high load where transient provider outages should trip the breaker, but bad prompts (400s) should not degrade capacity for valid requests.

---

## Solution 4: Contextual Fault Enrichment — Attach Debug Context at Classification Time

Enrich every classified fault with request context (prompt hash, model, token count) so post-mortem debugging doesn't require log correlation.

```python
import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic, APIStatusError

client = AsyncAnthropic()

@dataclass
class EnrichedFault:
    fault_class: str
    error_code: int | None
    error_message: str
    request_context: dict
    timestamp: float = field(default_factory=time.time)
    retryable: bool = False
    suggested_action: str = ""

    def as_log_record(self) -> dict:
        return {
            "fault_class": self.fault_class,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "suggested_action": self.suggested_action,
            "timestamp": self.timestamp,
            **{f"req_{k}": v for k, v in self.request_context.items()},
        }

def enrich_fault(exc: Exception, request_context: dict) -> EnrichedFault:
    base = {
        "fault_class": "unknown",
        "error_code": None,
        "error_message": str(exc),
        "request_context": request_context,
        "retryable": False,
        "suggested_action": "inspect logs",
    }

    if isinstance(exc, APIStatusError):
        base["error_code"] = exc.status_code
        if exc.status_code == 429:
            base.update({
                "fault_class": "quota",
                "retryable": True,
                "suggested_action": "wait for retry-after header, then retry",
            })
        elif exc.status_code >= 500:
            base.update({
                "fault_class": "transient",
                "retryable": True,
                "suggested_action": "retry with exponential backoff",
            })
        elif exc.status_code == 400:
            base.update({
                "fault_class": "fatal",
                "retryable": False,
                "suggested_action": "fix prompt parameters — check max_tokens, model name",
            })
        elif exc.status_code == 401:
            base.update({
                "fault_class": "fatal",
                "retryable": False,
                "suggested_action": "rotate API key — current key is invalid",
            })
        elif exc.status_code == 413:
            base.update({
                "fault_class": "fatal",
                "retryable": False,
                "suggested_action": "reduce prompt size below context limit",
            })

    return EnrichedFault(**base)

async def instrumented_call(prompt: str) -> dict:
    model = "claude-haiku-4-5-20251001"
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    context = {
        "model": model,
        "prompt_hash": prompt_hash,
        "prompt_len": len(prompt),
    }

    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return {"text": resp.content[0].text}

    except Exception as exc:
        fault = enrich_fault(exc, context)
        import logging
        logging.getLogger(__name__).error("api_fault", extra=fault.as_log_record())
        return {"error": fault.fault_class, "action": fault.suggested_action}
```

**When to use**: Agents in production where on-call engineers need to diagnose fault patterns quickly without raw log digging.

---

## Solution 5: Fault Budget — Stop Retrying When Budget Is Exhausted

Track per-session retry budget; once it's spent, fail fast instead of continuing to retry for this session.

```python
import asyncio
import time
from anthropic import AsyncAnthropic, APIStatusError, APITimeoutError

client = AsyncAnthropic()

class FaultBudget:
    """
    Session-scoped retry budget. Prevents one bad session from consuming
    all retry capacity (and hiding a systemic problem).
    """

    def __init__(self, max_retries: int = 10, window_seconds: float = 60.0):
        self._max_retries = max_retries
        self._used = 0
        self._window = window_seconds
        self._first_retry_at: float | None = None

    def consume(self) -> bool:
        """Returns True if a retry is allowed, False if budget exhausted."""
        now = time.monotonic()

        # Reset window if it's expired
        if self._first_retry_at and (now - self._first_retry_at) > self._window:
            self._used = 0
            self._first_retry_at = None

        if self._used == 0:
            self._first_retry_at = now

        if self._used >= self._max_retries:
            return False

        self._used += 1
        return True

    @property
    def remaining(self) -> int:
        return max(0, self._max_retries - self._used)

    @property
    def exhausted(self) -> bool:
        return self._used >= self._max_retries

def classify(exc: Exception) -> str:
    if isinstance(exc, APITimeoutError):
        return "transient"
    if isinstance(exc, APIStatusError):
        if exc.status_code == 429:
            return "quota"
        if exc.status_code >= 500:
            return "transient"
        return "fatal"
    return "unknown"

async def budget_aware_call(prompt: str, budget: FaultBudget) -> dict:
    delay = 1.0

    while True:
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return {"text": resp.content[0].text, "retries_used": budget._used}

        except Exception as exc:
            fault_class = classify(exc)

            if fault_class == "fatal":
                return {"error": "fatal", "message": str(exc)}

            if not budget.consume():
                return {
                    "error": "budget_exhausted",
                    "message": f"Used all {budget._max_retries} retries",
                    "last_error": str(exc),
                }

            wait = delay if fault_class != "quota" else float(
                getattr(exc, "response", None) and
                exc.response.headers.get("retry-after", delay) or delay
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30.0)

# Each session gets its own budget
async def handle_session(session_id: str, messages: list[str]):
    budget = FaultBudget(max_retries=10, window_seconds=60.0)
    results = []
    for msg in messages:
        result = await budget_aware_call(msg, budget)
        results.append(result)
        if budget.exhausted:
            results.append({"error": "session_budget_exhausted"})
            break
    return results
```

**When to use**: Multi-turn agent sessions where a persistent error should terminate the session rather than burning retry budget indefinitely.

---

## Solution 6: Fault Classification Middleware — Wrap Tool Calls Transparently

Apply fault classification to all tool calls via a middleware decorator, so new tools get correct retry behavior automatically.

```python
import asyncio
import functools
import logging
from typing import Callable, TypeVar, ParamSpec
from anthropic import AsyncAnthropic, APIStatusError, APITimeoutError

client = AsyncAnthropic()
logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

def with_fault_classification(
    max_retries: int = 3,
    base_delay: float = 1.0,
    fatal_codes: frozenset = frozenset({400, 401, 403, 422}),
):
    """
    Decorator that classifies errors and applies the correct retry policy.
    Wraps any async function that may raise APIStatusError or APITimeoutError.
    """
    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            delay = base_delay
            for attempt in range(max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except APITimeoutError as exc:
                    if attempt >= max_retries:
                        raise
                    logger.warning("transient_timeout", extra={"attempt": attempt, "fn": fn.__name__})
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)

                except APIStatusError as exc:
                    if exc.status_code in fatal_codes:
                        logger.error("fatal_fault", extra={"status": exc.status_code, "fn": fn.__name__})
                        raise  # do not retry
                    if exc.status_code == 429:
                        retry_after = float(exc.response.headers.get("retry-after", delay))
                        logger.warning("quota_fault", extra={"retry_after": retry_after, "fn": fn.__name__})
                        await asyncio.sleep(retry_after)
                        continue
                    if exc.status_code >= 500:
                        if attempt >= max_retries:
                            raise
                        logger.warning("transient_server_error", extra={"status": exc.status_code})
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 30.0)
                    else:
                        raise
        return wrapper
    return decorator

# Apply to any tool-calling function
@with_fault_classification(max_retries=3, base_delay=1.0)
async def call_search_tool(query: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[{
            "name": "search",
            "description": "Search the web",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        }],
        messages=[{"role": "user", "content": f"Search for: {query}"}],
    )
    return resp.content[0].text

@with_fault_classification(max_retries=5, base_delay=2.0)
async def call_llm(prompt: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text
```

**When to use**: Codebases with many tool-calling functions. The decorator ensures consistent fault handling without duplicating retry logic in every function.

---

## Comparison

| Solution | Retry Policy | Fatal Detection | Context | Budget Control | Ease of Extension | Best For |
|---|---|---|---|---|---|---|
| Exception taxonomy | Per class | Yes | No | No | Manual | New projects, explicit control |
| Fault registry | Per rule | Yes | No | No | Table edit | Teams updating rules frequently |
| Fault-aware circuit breaker | Transient only | Yes | No | No | Medium | High-load agents |
| Contextual enrichment | Per class | Yes | Yes | No | Low | Debugging production incidents |
| Fault budget | Per session | Yes | No | Yes | Medium | Multi-turn sessions |
| Middleware decorator | Per decorator | Yes | No | No | Very low | Many tool-calling functions |

**Rule of thumb**: Always classify 401/403/400 as fatal (never retry). Classify 429 as quota (use Retry-After header). Classify 5xx and timeouts as transient (exponential backoff). Everything else: retry once, then surface to caller.
