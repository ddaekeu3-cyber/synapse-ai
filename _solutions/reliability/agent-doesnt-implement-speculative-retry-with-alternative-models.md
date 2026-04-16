---
title: "Agent Doesn't Implement Speculative Retry with Alternative Models"
description: "Agents that retry failed LLM calls against the same model waste time waiting through backoff intervals while an available alternative model could respond immediately. Implement speculative retry to simultaneously attempt the original model (with backoff) and a fallback model, returning whichever responds first with a valid result — minimizing latency impact from primary model degradation."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-speculative-retry-with-alternative-models
tags: [speculative-retry, model-fallback, hedging, resilience, latency, multi-model]
symptoms:
  - "Primary model returns 503 — agent waits 30 seconds for backoff before trying the next model"
  - "Fallback model is available immediately but agent serializes through a fixed retry chain"
  - "No hedging — agent never issues parallel requests to two models simultaneously"
  - "Model degradation causes 60-second p99 latency spikes instead of transparent failover"
  - "Fallback only activates after all retries against the primary model are exhausted"
---

## Why This Happens

Standard retry logic is sequential: try model A, wait, try model A again, wait, then try model B. Speculative retry runs the primary model attempt and a fallback attempt in parallel after a short hedge delay — if the primary responds before the hedge delay expires, the fallback is cancelled; if the primary is slow or failed, the fallback response arrives without waiting through full backoff. This is the same technique used by Google's tail-latency hedging: the marginal cost of the speculative request is low, and the latency benefit for degraded primary scenarios is high.

## Solution 1: Model Candidate Chain

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ModelCandidate:
    model_id: str
    provider: str          # "anthropic" | "openai" | "google" etc.
    priority: int          # lower = preferred (0 = primary)
    max_tokens: int = 4096
    temperature: float = 0.7
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    avg_latency_ms: float = 0.0   # tracked at runtime
    error_rate: float = 0.0       # tracked at runtime
    last_error_at: float = 0.0
    consecutive_errors: int = 0

    def is_available(self, cooldown_seconds: float = 60.0) -> bool:
        if self.consecutive_errors >= 5:
            if time.time() - self.last_error_at < cooldown_seconds:
                return False
        return True

    def record_success(self, latency_ms: float) -> None:
        alpha = 0.1
        self.avg_latency_ms = (
            latency_ms if self.avg_latency_ms == 0
            else self.avg_latency_ms * (1 - alpha) + latency_ms * alpha
        )
        self.consecutive_errors = 0
        self.error_rate = max(0.0, self.error_rate - 0.05)

    def record_error(self) -> None:
        self.consecutive_errors += 1
        self.last_error_at = time.time()
        self.error_rate = min(1.0, self.error_rate + 0.2)

@dataclass
class ModelChain:
    candidates: List[ModelCandidate] = field(default_factory=list)

    def available_candidates(self) -> List[ModelCandidate]:
        return sorted(
            [c for c in self.candidates if c.is_available()],
            key=lambda c: c.priority,
        )

    def primary(self) -> Optional[ModelCandidate]:
        available = self.available_candidates()
        return available[0] if available else None

    def fallbacks(self) -> List[ModelCandidate]:
        available = self.available_candidates()
        return available[1:]
```

## Solution 2: Speculative Model Caller

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, List, Optional, Tuple

@dataclass
class ModelCallResult:
    model_id: str
    response: Any
    latency_ms: float
    was_speculative: bool = False
    was_primary: bool = True

class SpeculativeModelCaller:
    """
    Calls the primary model and, after a hedge delay, speculatively
    starts the first available fallback. Returns whichever completes
    first with a valid (non-error) response. Cancels the slower one.
    """

    def __init__(
        self,
        call_fn: Callable[[ModelCandidate, Any], Coroutine],
        hedge_delay_ms: float = 200.0,
        validate_fn: Optional[Callable[[Any], bool]] = None,
    ):
        self._call_fn = call_fn
        self._hedge_delay = hedge_delay_ms / 1000.0
        self._validate = validate_fn or (lambda r: r is not None)
        self._speculative_calls = 0
        self._speculative_wins = 0

    async def call(
        self,
        chain: ModelChain,
        request: Any,
    ) -> ModelCallResult:
        candidates = chain.available_candidates()
        if not candidates:
            raise RuntimeError("no available model candidates")

        primary = candidates[0]
        fallbacks = candidates[1:]

        primary_task = asyncio.ensure_future(
            self._timed_call(primary, request, is_primary=True)
        )

        if not fallbacks:
            # No fallback — just await primary
            return await primary_task

        # Start fallback after hedge delay
        hedge_task = asyncio.ensure_future(
            self._hedged_call(fallbacks[0], request, self._hedge_delay)
        )
        self._speculative_calls += 1

        try:
            done, pending = await asyncio.wait(
                [primary_task, hedge_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Try to get a valid result from completed tasks
            for task in done:
                result = task.result()
                if result and self._validate(result.response):
                    for p in pending:
                        p.cancel()
                    if not result.was_primary:
                        self._speculative_wins += 1
                    return result

            # If first completed wasn't valid, wait for the other
            remaining = list(pending)
            if remaining:
                result = await remaining[0]
                # Cancel any others
                for p in remaining[1:]:
                    p.cancel()
                return result

        except Exception:
            primary_task.cancel()
            hedge_task.cancel()
            raise

        raise RuntimeError("all model candidates failed")

    async def _timed_call(
        self,
        candidate: ModelCandidate,
        request: Any,
        is_primary: bool,
    ) -> Optional[ModelCallResult]:
        t0 = time.monotonic()
        try:
            response = await self._call_fn(candidate, request)
            latency_ms = (time.monotonic() - t0) * 1000
            candidate.record_success(latency_ms)
            return ModelCallResult(
                model_id=candidate.model_id,
                response=response,
                latency_ms=latency_ms,
                was_primary=is_primary,
                was_speculative=not is_primary,
            )
        except Exception:
            candidate.record_error()
            return None

    async def _hedged_call(
        self,
        candidate: ModelCandidate,
        request: Any,
        delay: float,
    ) -> Optional[ModelCallResult]:
        await asyncio.sleep(delay)
        return await self._timed_call(candidate, request, is_primary=False)

    def speculative_win_rate(self) -> float:
        return round(
            self._speculative_wins / max(self._speculative_calls, 1), 4
        )
```

## Solution 3: Retry Budget with Fallback Escalation

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, List, Optional

@dataclass
class RetryState:
    attempt: int = 0
    last_error: Optional[Exception] = None
    last_model: str = ""
    start_time: float = field(default_factory=time.time)
    total_latency_ms: float = 0.0

class RetryBudgetWithFallback:
    """
    Implements a retry budget across a model chain.
    Each model gets at most max_attempts_per_model attempts.
    On exhaustion, escalates to the next model in the chain.
    Tracks total budget (across all models) to prevent infinite retries.
    """

    def __init__(
        self,
        call_fn: Callable[[ModelCandidate, Any], Coroutine],
        max_attempts_per_model: int = 2,
        total_budget: int = 6,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
    ):
        self._call_fn = call_fn
        self._max_per_model = max_attempts_per_model
        self._total_budget = total_budget
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds

    async def execute(
        self,
        chain: ModelChain,
        request: Any,
    ) -> tuple:
        """Returns (result, state)."""
        state = RetryState()
        candidates = chain.available_candidates()
        if not candidates:
            raise RuntimeError("no model candidates available")

        for candidate in candidates:
            model_attempts = 0
            while model_attempts < self._max_per_model:
                if state.attempt >= self._total_budget:
                    raise RuntimeError(
                        f"total retry budget of {self._total_budget} exhausted"
                    )

                state.attempt += 1
                state.last_model = candidate.model_id
                t0 = time.monotonic()

                try:
                    result = await self._call_fn(candidate, request)
                    state.total_latency_ms += (time.monotonic() - t0) * 1000
                    candidate.record_success(state.total_latency_ms)
                    return result, state
                except Exception as exc:
                    state.last_error = exc
                    state.total_latency_ms += (time.monotonic() - t0) * 1000
                    candidate.record_error()
                    model_attempts += 1

                    if model_attempts < self._max_per_model:
                        delay = min(
                            self._base_delay * (2 ** (model_attempts - 1)),
                            self._max_delay,
                        )
                        await asyncio.sleep(delay)

        raise RuntimeError(
            f"all candidates exhausted after {state.attempt} attempts: {state.last_error}"
        )
```

## Solution 4: Response Quality Validator

```python
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

@dataclass
class ValidationResult:
    valid: bool
    score: float      # 0.0–1.0
    failures: List[str]
    model_id: str

class ResponseQualityValidator:
    """
    Validates model responses against quality criteria before accepting them.
    A speculative retry result from a cheaper model should only be accepted
    if it meets minimum quality requirements — otherwise fall through to primary.
    """

    def __init__(
        self,
        min_length: int = 10,
        max_length: int = 100_000,
        required_fields: Optional[List[str]] = None,
        forbidden_patterns: Optional[List[str]] = None,
    ):
        self._min_len = min_length
        self._max_len = max_length
        self._required_fields = required_fields or []
        self._forbidden = [re.compile(p) for p in (forbidden_patterns or [])]

    def validate(self, response: Any, model_id: str = "") -> ValidationResult:
        failures = []

        if isinstance(response, str):
            text = response
        elif isinstance(response, dict):
            text = response.get("content", response.get("text", str(response)))
        else:
            text = str(response)

        if len(text) < self._min_len:
            failures.append(f"response too short: {len(text)} < {self._min_len}")
        if len(text) > self._max_len:
            failures.append(f"response too long: {len(text)} > {self._max_len}")

        for field in self._required_fields:
            if field not in text:
                failures.append(f"missing required field: {field}")

        for pattern in self._forbidden:
            if pattern.search(text):
                failures.append(f"forbidden pattern found: {pattern.pattern}")

        score = max(0.0, 1.0 - len(failures) * 0.2)
        return ValidationResult(
            valid=len(failures) == 0,
            score=score,
            failures=failures,
            model_id=model_id,
        )
```

## Solution 5: Speculative Retry Metrics

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List

@dataclass
class RetryEvent:
    primary_model: str
    used_model: str
    was_speculative_win: bool
    total_latency_ms: float
    attempts: int
    timestamp: float

class SpeculativeRetryMetrics:
    """
    Tracks speculative retry outcomes to measure hedge effectiveness.
    Reports: speculative win rate, average latency savings, per-model error rates.
    """

    def __init__(self):
        self._events: Deque[RetryEvent] = deque(maxlen=5000)
        self._model_calls: Dict[str, int] = defaultdict(int)
        self._model_errors: Dict[str, int] = defaultdict(int)

    def record(
        self,
        primary_model: str,
        used_model: str,
        was_speculative_win: bool,
        total_latency_ms: float,
        attempts: int,
    ) -> None:
        event = RetryEvent(
            primary_model=primary_model,
            used_model=used_model,
            was_speculative_win=was_speculative_win,
            total_latency_ms=total_latency_ms,
            attempts=attempts,
            timestamp=time.time(),
        )
        self._events.append(event)
        self._model_calls[used_model] += 1

    def record_model_error(self, model_id: str) -> None:
        self._model_errors[model_id] += 1

    def report(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.timestamp >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "events": 0}

        spec_wins = [e for e in recent if e.was_speculative_win]
        fallback_used = [e for e in recent if e.primary_model != e.used_model]

        avg_latency = sum(e.total_latency_ms for e in recent) / len(recent)
        primary_only = [e for e in recent if not e.was_speculative_win]
        avg_primary_latency = (
            sum(e.total_latency_ms for e in primary_only) / len(primary_only)
            if primary_only else 0.0
        )
        avg_spec_latency = (
            sum(e.total_latency_ms for e in spec_wins) / len(spec_wins)
            if spec_wins else 0.0
        )

        return {
            "events": len(recent),
            "speculative_wins": len(spec_wins),
            "speculative_win_rate": round(len(spec_wins) / len(recent), 4),
            "fallback_rate": round(len(fallback_used) / len(recent), 4),
            "avg_latency_ms": round(avg_latency, 1),
            "avg_primary_only_latency_ms": round(avg_primary_latency, 1),
            "avg_speculative_win_latency_ms": round(avg_spec_latency, 1),
            "model_error_rates": {
                m: round(self._model_errors[m] / max(self._model_calls[m], 1), 4)
                for m in self._model_calls
            },
        }
```

## Solution 6: Adaptive Hedge Delay Tuner

```python
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

@dataclass
class HeadgeSample:
    primary_latency_ms: float
    hedge_fired: bool
    hedge_won: bool
    timestamp: float

class AdaptiveHedgeDelayTuner:
    """
    Adjusts the hedge delay based on observed primary model latency distribution.
    Sets the hedge delay to the p50 primary latency — hedge fires only when primary
    is slower than usual, minimizing unnecessary speculative calls.
    """

    def __init__(
        self,
        initial_delay_ms: float = 200.0,
        min_delay_ms: float = 50.0,
        max_delay_ms: float = 2000.0,
        history_size: int = 200,
    ):
        self._delay_ms = initial_delay_ms
        self._min = min_delay_ms
        self._max = max_delay_ms
        self._history: Deque[HeadgeSample] = deque(maxlen=history_size)

    def record(
        self,
        primary_latency_ms: float,
        hedge_fired: bool,
        hedge_won: bool,
    ) -> None:
        self._history.append(HeadgeSample(
            primary_latency_ms=primary_latency_ms,
            hedge_fired=hedge_fired,
            hedge_won=hedge_won,
            timestamp=time.time(),
        ))
        self._recompute()

    def _recompute(self) -> None:
        if len(self._history) < 20:
            return
        latencies = sorted(s.primary_latency_ms for s in self._history)
        p50 = latencies[len(latencies) // 2]
        # Hedge at p50: fire only when primary is slower than median
        new_delay = max(self._min, min(self._max, p50))
        # Smooth adjustment
        self._delay_ms = self._delay_ms * 0.8 + new_delay * 0.2

    @property
    def current_delay_ms(self) -> float:
        return round(self._delay_ms, 1)

    def stats(self) -> dict:
        if not self._history:
            return {"delay_ms": self._delay_ms}
        latencies = [s.primary_latency_ms for s in self._history]
        hedge_fired = sum(1 for s in self._history if s.hedge_fired)
        hedge_won = sum(1 for s in self._history if s.hedge_won)
        return {
            "current_delay_ms": round(self._delay_ms, 1),
            "p50_primary_latency_ms": round(sorted(latencies)[len(latencies) // 2], 1),
            "p95_primary_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 1),
            "hedge_fire_rate": round(hedge_fired / len(self._history), 4),
            "hedge_win_rate": round(hedge_won / max(hedge_fired, 1), 4),
        }
```

## Comparison

| Approach | Parallel Hedging | Sequential Fallback | Latency-Aware | Quality Check |
|---|---|---|---|---|
| SpeculativeModelCaller | Yes (hedge delay) | No | Via hedge delay | Via validate_fn |
| RetryBudgetWithFallback | No | Yes | No | No |
| ResponseQualityValidator | N/A | N/A | No | Yes |
| AdaptiveHedgeDelayTuner | Via caller | N/A | Yes (p50-based) | No |
| SpeculativeRetryMetrics | N/A | N/A | Yes (reported) | N/A |

**Best for production**: Use `SpeculativeModelCaller` as the default LLM invocation path. Set initial hedge delay to 200ms — this is well below most LLM p50 latencies, so hedge fires only during degradation. Wire `AdaptiveHedgeDelayTuner` to adjust the delay based on observed primary latency; at healthy p50 = 800ms, the tuner sets hedge delay to ~800ms — eliminating unnecessary speculative calls. Validate all fallback responses with `ResponseQualityValidator` to prevent accepting truncated or error responses from cheaper models. Monitor `SpeculativeRetryMetrics.report()` — speculative win rates above 5% indicate primary model degradation worth investigating.
