---
title: "Agent Doesn't Implement Prompt Length Rate Limiting Per Session"
description: "Agents that accept arbitrarily long prompts without per-session rate limiting are vulnerable to token exhaustion attacks: a single session submits a 500,000-token prompt that costs $50 and occupies the model for minutes, or submits 1,000 small prompts in an hour driving up costs and crowding out legitimate users. Implement per-session prompt length rate limiting that enforces token budgets over sliding windows and blocks sessions that exceed them."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-prompt-length-rate-limiting-per-session
tags: [rate-limiting, token-budget, prompt-length, session-limits, cost-protection, abuse-prevention]
symptoms:
  - "Single session submits 200k-token prompt causing $20 model call and 30-second response"
  - "No per-session token consumption tracking — all sessions share a global limit"
  - "Automated scripts submit thousands of small prompts per hour without throttling"
  - "No distinction between a 100-token and 100,000-token prompt in admission control"
  - "Token cost spikes traced to one abusive session discovered only in billing review"
---

## Why This Happens

Request-count rate limiting (N requests per minute) is insufficient for LLM agents because a single request can consume a thousand times more tokens than another. A session that submits one 200,000-token prompt uses the same compute as 2,000 normal requests but passes a request-count rate limiter easily. Token-aware rate limiting requires estimating or measuring the token count of each prompt before dispatch, accumulating per-session token consumption in a sliding window, and blocking or throttling sessions that exceed their budget.

## Solution 1: Session Token Budget

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionTokenBudget:
    session_id: str
    max_tokens_per_window: int = 100_000      # total input tokens per window
    max_tokens_per_request: int = 32_000      # single request ceiling
    window_seconds: float = 3600.0            # sliding window duration
    burst_tokens: int = 10_000                # allowed burst above rate
    warn_threshold_pct: float = 0.80          # warn at 80% of budget
```

## Solution 2: Session Token Consumption Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class SessionTokenConsumptionTracker:
    """
    Tracks per-session token consumption in a sliding window.
    Thread-safe; one instance per session.
    """

    def __init__(self, budget: SessionTokenBudget):
        self._budget = budget
        self._events: Deque[Tuple[float, int]] = deque()
        # (timestamp, token_count)
        self._lock = Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self._budget.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def consumed(self) -> int:
        now = time.time()
        with self._lock:
            self._prune(now)
            return sum(count for _, count in self._events)

    def record(self, tokens: int) -> None:
        with self._lock:
            self._events.append((time.time(), tokens))
            self._prune(time.time())

    def remaining(self) -> int:
        return max(0, self._budget.max_tokens_per_window - self.consumed())

    def utilization(self) -> float:
        return round(self.consumed() / self._budget.max_tokens_per_window, 4)

    def snapshot(self) -> dict:
        consumed = self.consumed()
        return {
            "session_id": self._budget.session_id,
            "consumed_tokens": consumed,
            "budget_tokens": self._budget.max_tokens_per_window,
            "remaining_tokens": max(0, self._budget.max_tokens_per_window - consumed),
            "utilization": round(consumed / self._budget.max_tokens_per_window, 4),
            "window_seconds": self._budget.window_seconds,
        }
```

## Solution 3: Prompt Length Rate Limiter

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimitDecision:
    allowed: bool
    session_id: str
    requested_tokens: int
    consumed_tokens: int
    remaining_tokens: int
    reason: str = ""
    warning: str = ""


class PromptLengthRateLimiter:
    """
    Checks a prompt's token estimate against the session's sliding window budget.
    Returns a RateLimitDecision without blocking — caller enforces the decision.
    """

    def __init__(self, tokens_per_char: float = 0.25):
        self._tokens_per_char = tokens_per_char

    def estimate_tokens(self, prompt: str) -> int:
        return max(1, int(len(prompt) * self._tokens_per_char))

    def check(
        self,
        prompt: str,
        tracker: SessionTokenConsumptionTracker,
        token_count: Optional[int] = None,
    ) -> RateLimitDecision:
        budget = tracker._budget
        tokens = token_count or self.estimate_tokens(prompt)
        consumed = tracker.consumed()
        remaining = max(0, budget.max_tokens_per_window - consumed)

        # Single-request ceiling
        if tokens > budget.max_tokens_per_request:
            return RateLimitDecision(
                allowed=False,
                session_id=budget.session_id,
                requested_tokens=tokens,
                consumed_tokens=consumed,
                remaining_tokens=remaining,
                reason=f"Single request {tokens} tokens exceeds per-request limit {budget.max_tokens_per_request}",
            )

        # Window budget
        if tokens > remaining + budget.burst_tokens:
            return RateLimitDecision(
                allowed=False,
                session_id=budget.session_id,
                requested_tokens=tokens,
                consumed_tokens=consumed,
                remaining_tokens=remaining,
                reason=f"Session token budget exhausted: {consumed}/{budget.max_tokens_per_window} used",
            )

        warning = ""
        if (consumed + tokens) / budget.max_tokens_per_window >= budget.warn_threshold_pct:
            warning = f"Session at {int((consumed + tokens) / budget.max_tokens_per_window * 100)}% of token budget"

        return RateLimitDecision(
            allowed=True,
            session_id=budget.session_id,
            requested_tokens=tokens,
            consumed_tokens=consumed,
            remaining_tokens=remaining,
            warning=warning,
        )
```

## Solution 4: Per-Session Rate Limit Registry

```python
from threading import Lock
from typing import Dict, Optional


class PerSessionRateLimitRegistry:
    """
    Manages one SessionTokenConsumptionTracker per session.
    Creates trackers lazily with a default budget.
    """

    def __init__(
        self,
        default_budget_tokens: int = 100_000,
        default_window_seconds: float = 3600.0,
        default_max_per_request: int = 32_000,
    ):
        self._defaults = {
            "max_tokens_per_window": default_budget_tokens,
            "window_seconds": default_window_seconds,
            "max_tokens_per_request": default_max_per_request,
        }
        self._trackers: Dict[str, SessionTokenConsumptionTracker] = {}
        self._lock = Lock()

    def get_or_create(self, session_id: str) -> SessionTokenConsumptionTracker:
        with self._lock:
            if session_id not in self._trackers:
                budget = SessionTokenBudget(session_id=session_id, **self._defaults)
                self._trackers[session_id] = SessionTokenConsumptionTracker(budget)
            return self._trackers[session_id]

    def evict(self, session_id: str) -> None:
        with self._lock:
            self._trackers.pop(session_id, None)

    def all_snapshots(self) -> Dict[str, dict]:
        with self._lock:
            return {sid: t.snapshot() for sid, t in self._trackers.items()}
```

## Solution 5: Rate-Limited Prompt Gate

```python
import time
from typing import Optional


class TokenBudgetExceededError(Exception):
    def __init__(self, decision: RateLimitDecision):
        super().__init__(decision.reason)
        self.decision = decision


class RateLimitedPromptGate:
    """
    Enforces prompt length rate limits before a prompt reaches the LLM.
    Records consumption on allow and raises on deny.
    """

    def __init__(
        self,
        registry: PerSessionRateLimitRegistry,
        limiter: PromptLengthRateLimiter,
        audit_log_fn=None,
    ):
        self._registry = registry
        self._limiter = limiter
        self._audit_log = audit_log_fn
        self._blocked = 0
        self._allowed = 0

    def admit(
        self,
        session_id: str,
        prompt: str,
        token_count: Optional[int] = None,
    ) -> RateLimitDecision:
        tracker = self._registry.get_or_create(session_id)
        decision = self._limiter.check(prompt, tracker, token_count)

        if decision.allowed:
            tracker.record(decision.requested_tokens)
            self._allowed += 1
        else:
            self._blocked += 1
            if self._audit_log:
                self._audit_log({
                    "event": "prompt_rate_limited",
                    "ts": time.time(),
                    "session_id": session_id,
                    "requested_tokens": decision.requested_tokens,
                    "reason": decision.reason,
                })
            raise TokenBudgetExceededError(decision)

        return decision

    def stats(self) -> dict:
        return {
            "allowed": self._allowed,
            "blocked": self._blocked,
            "block_rate": round(self._blocked / max(self._allowed + self._blocked, 1), 4),
        }
```

## Solution 6: Token Budget Abuse Detector

```python
import time
from threading import Lock
from typing import Dict, List


class TokenBudgetAbuseDetector:
    """
    Identifies sessions that repeatedly hit rate limits — indicating
    automated abuse rather than organic high-volume use.
    """

    def __init__(self, block_count_threshold: int = 5, window_seconds: float = 3600.0):
        self._threshold = block_count_threshold
        self._window = window_seconds
        self._blocks: Dict[str, List[float]] = {}
        self._lock = Lock()

    def record_block(self, session_id: str) -> None:
        now = time.time()
        with self._lock:
            events = self._blocks.setdefault(session_id, [])
            events.append(now)
            cutoff = now - self._window
            self._blocks[session_id] = [t for t in events if t >= cutoff]

    def is_abusive(self, session_id: str) -> bool:
        with self._lock:
            return len(self._blocks.get(session_id, [])) >= self._threshold

    def abusive_sessions(self) -> List[str]:
        with self._lock:
            return [sid for sid, events in self._blocks.items() if len(events) >= self._threshold]

    def summary(self) -> dict:
        with self._lock:
            abusive = [sid for sid, ev in self._blocks.items() if len(ev) >= self._threshold]
            return {
                "tracked_sessions": len(self._blocks),
                "abusive_sessions": len(abusive),
                "session_ids": abusive,
            }
```

## Comparison

| Approach | Per-Request Ceiling | Sliding Window | Per-Session Tracking | Abuse Detection | Audit Log |
|---|---|---|---|---|---|
| PromptLengthRateLimiter | Yes | Via tracker | Via tracker | No | No |
| SessionTokenConsumptionTracker | No | Yes | Yes (single session) | No | No |
| PerSessionRateLimitRegistry | No | Via tracker | Yes (all sessions) | No | No |
| RateLimitedPromptGate | Via limiter | Via registry | Via registry | No | Yes |
| TokenBudgetAbuseDetector | No | No | No | Yes | No |

**Best for production**: Set `max_tokens_per_request=32000` as the single-request ceiling — prompts longer than this are almost always automated abuse or misconfigured clients, not human users. Use `max_tokens_per_window=100000` with a 1-hour sliding window for authenticated users; anonymous sessions should get 20% of that. Integrate `TokenBudgetAbuseDetector` with your session ban system: 5+ blocks in an hour from one session warrants automatic suspension. Monitor `block_rate` via `RateLimitedPromptGate.stats()`: a sustained block rate above 2% means the budget is too tight for legitimate users and needs adjustment.
