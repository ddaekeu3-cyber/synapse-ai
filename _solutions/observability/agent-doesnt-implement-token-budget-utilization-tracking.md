---
title: "Agent Doesn't Implement Token Budget Utilization Tracking"
description: "Agents that make LLM calls without tracking token consumption cannot detect runaway context growth, optimize prompt efficiency, or enforce per-user token budgets. Implement token budget utilization tracking that measures input and output tokens per call, accumulates usage per session and user, surfaces budget burn rate, and alerts when utilization approaches limits."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-budget-utilization-tracking
tags: [token-budget, token-tracking, cost-observability, context-window, llm-usage, budget-alerts]
symptoms:
  - "No record of how many tokens each LLM call consumed"
  - "Cannot determine which conversations are consuming disproportionate token budget"
  - "Token limit errors arrive with no prior warning — no burn rate tracking"
  - "Per-user token costs are unknown — cannot implement fair usage policies"
  - "Prompt engineering improvements cannot be measured without token usage baselines"
---

## Why This Happens

LLM APIs return token usage in every response, but agents that treat responses as opaque text blobs discard this data immediately. Without accumulating usage statistics, there is no way to compute burn rate, project when a session will exhaust its budget, or compare token efficiency across prompt versions. Token budget tracking requires capturing usage from every API response, associating it with a session and user, and exposing aggregated views that operators can act on.

## Solution 1: Token Usage Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenUsageRecord:
    session_id: str
    user_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    timestamp: float = field(default_factory=time.time)
    call_purpose: str = ""          # "tool_call", "synthesis", "summarization"
    latency_ms: float = 0.0
    cached_tokens: int = 0          # tokens served from prompt cache

    @property
    def cost_units(self) -> float:
        """Normalized cost unit: 1 unit = 1K tokens."""
        return self.total_tokens / 1000.0


@dataclass
class TokenBudget:
    session_budget: int             # max tokens per conversation
    user_daily_budget: int          # max tokens per user per day
    alert_threshold_fraction: float = 0.80  # alert when this fraction is consumed
```

## Solution 2: Session Token Accumulator

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import List, Optional


@dataclass
class SessionTokenState:
    session_id: str
    user_id: str
    started_at: float = field(default_factory=time.time)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    peak_single_call_tokens: int = 0
    records: List[TokenUsageRecord] = field(default_factory=list)

    def burn_rate_tokens_per_minute(self) -> float:
        elapsed = (time.time() - self.started_at) / 60.0
        if elapsed < 0.1:
            return 0.0
        return round(self.total_tokens / elapsed, 1)

    def projected_total(self, expected_duration_minutes: float) -> int:
        return int(self.burn_rate_tokens_per_minute() * expected_duration_minutes)


class SessionTokenAccumulator:
    """
    Tracks token usage per session. Thread-safe accumulation of
    token records with burn rate and projection support.
    """

    def __init__(self):
        self._sessions: dict = {}
        self._lock = Lock()

    def record(self, usage: TokenUsageRecord) -> SessionTokenState:
        with self._lock:
            if usage.session_id not in self._sessions:
                self._sessions[usage.session_id] = SessionTokenState(
                    session_id=usage.session_id,
                    user_id=usage.user_id,
                )
            state = self._sessions[usage.session_id]
            state.total_prompt_tokens += usage.prompt_tokens
            state.total_completion_tokens += usage.completion_tokens
            state.total_tokens += usage.total_tokens
            state.call_count += 1
            state.peak_single_call_tokens = max(
                state.peak_single_call_tokens, usage.total_tokens
            )
            state.records.append(usage)
            return state

    def get(self, session_id: str) -> Optional[SessionTokenState]:
        with self._lock:
            return self._sessions.get(session_id)

    def all_sessions(self) -> List[SessionTokenState]:
        with self._lock:
            return list(self._sessions.values())
```

## Solution 3: User Daily Token Ledger

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class UserDailyTokenLedger:
    """
    Tracks cumulative token usage per user within a rolling 24-hour window.
    Supports budget checks and per-user utilization reporting.
    """

    def __init__(self, default_daily_budget: int = 500_000):
        self._default_budget = default_daily_budget
        self._budgets: Dict[str, int] = {}
        self._records: Dict[str, List[TokenUsageRecord]] = defaultdict(list)
        self._lock = Lock()

    def set_budget(self, user_id: str, budget: int) -> None:
        with self._lock:
            self._budgets[user_id] = budget

    def record(self, usage: TokenUsageRecord) -> None:
        with self._lock:
            self._records[usage.user_id].append(usage)

    def _window_total(self, user_id: str, window_seconds: float = 86400.0) -> int:
        cutoff = time.time() - window_seconds
        return sum(
            r.total_tokens
            for r in self._records.get(user_id, [])
            if r.timestamp >= cutoff
        )

    def check_budget(self, user_id: str, requested_tokens: int = 0) -> dict:
        budget = self._budgets.get(user_id, self._default_budget)
        used = self._window_total(user_id)
        remaining = max(0, budget - used)
        utilization = used / budget if budget > 0 else 1.0
        return {
            "user_id": user_id,
            "budget": budget,
            "used_24h": used,
            "remaining": remaining,
            "utilization": round(utilization, 4),
            "within_budget": used + requested_tokens <= budget,
        }

    def top_consumers(self, top_n: int = 10, window_seconds: float = 86400.0) -> List[dict]:
        with self._lock:
            user_ids = list(self._records.keys())
        return sorted(
            [
                {"user_id": uid, "tokens_24h": self._window_total(uid, window_seconds)}
                for uid in user_ids
            ],
            key=lambda x: -x["tokens_24h"],
        )[:top_n]
```

## Solution 4: Token Budget Alert Manager

```python
import time
from typing import Callable, List, Optional


class TokenBudgetAlert:
    def __init__(self, session_id: str, user_id: str, utilization: float, message: str):
        self.session_id = session_id
        self.user_id = user_id
        self.utilization = utilization
        self.message = message
        self.timestamp = time.time()


class TokenBudgetAlertManager:
    """
    Evaluates session and user token states against configured budgets
    and fires alerts when thresholds are crossed.
    """

    def __init__(
        self,
        session_budget: int = 200_000,
        alert_threshold: float = 0.80,
        alert_fn: Optional[Callable[[TokenBudgetAlert], None]] = None,
    ):
        self._session_budget = session_budget
        self._threshold = alert_threshold
        self._alert_fn = alert_fn or (lambda a: None)
        self._fired: List[TokenBudgetAlert] = []
        self._alerted_sessions: set = set()

    def evaluate_session(self, state: SessionTokenState) -> Optional[TokenBudgetAlert]:
        utilization = state.total_tokens / self._session_budget
        if utilization >= self._threshold and state.session_id not in self._alerted_sessions:
            alert = TokenBudgetAlert(
                session_id=state.session_id,
                user_id=state.user_id,
                utilization=round(utilization, 4),
                message=(
                    f"Session '{state.session_id}' has consumed "
                    f"{state.total_tokens:,} / {self._session_budget:,} tokens "
                    f"({utilization:.0%}) — burn rate {state.burn_rate_tokens_per_minute():.0f} tok/min"
                ),
            )
            self._alerted_sessions.add(state.session_id)
            self._fired.append(alert)
            self._alert_fn(alert)
            return alert
        return None

    def recent_alerts(self, window_seconds: float = 3600.0) -> List[TokenBudgetAlert]:
        cutoff = time.time() - window_seconds
        return [a for a in self._fired if a.timestamp >= cutoff]
```

## Solution 5: LLM Call Token Interceptor

```python
import time
from typing import Any, Callable, Dict, Optional


class LLMCallTokenInterceptor:
    """
    Wraps LLM API calls to extract token usage from responses
    and route records to the accumulator and ledger automatically.
    """

    def __init__(
        self,
        accumulator: SessionTokenAccumulator,
        ledger: UserDailyTokenLedger,
        alert_manager: TokenBudgetAlertManager,
    ):
        self._accumulator = accumulator
        self._ledger = ledger
        self._alerts = alert_manager

    async def call(
        self,
        llm_fn: Callable,
        session_id: str,
        user_id: str,
        model: str,
        call_purpose: str = "",
        **kwargs: Any,
    ) -> Any:
        start = time.time()
        response = await llm_fn(**kwargs)
        latency_ms = (time.time() - start) * 1000

        # Extract usage — supports Anthropic and OpenAI response shapes
        usage = getattr(response, "usage", None) or {}
        if hasattr(usage, "__dict__"):
            usage = usage.__dict__

        prompt_tokens = usage.get("input_tokens") or usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("output_tokens") or usage.get("completion_tokens", 0)
        cached_tokens = usage.get("cache_read_input_tokens", 0)

        record = TokenUsageRecord(
            session_id=session_id,
            user_id=user_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=round(latency_ms, 2),
            call_purpose=call_purpose,
            cached_tokens=cached_tokens,
        )

        state = self._accumulator.record(record)
        self._ledger.record(record)
        self._alerts.evaluate_session(state)

        return response
```

## Solution 6: Token Budget Utilization Dashboard

```python
import time


class TokenBudgetUtilizationDashboard:
    """
    Combines session-level burn rates, user-level daily consumption,
    and recent alerts into a single operational view.
    """

    def __init__(
        self,
        accumulator: SessionTokenAccumulator,
        ledger: UserDailyTokenLedger,
        alert_manager: TokenBudgetAlertManager,
    ):
        self._accumulator = accumulator
        self._ledger = ledger
        self._alerts = alert_manager

    def render(self) -> dict:
        sessions = self._accumulator.all_sessions()
        active = [s for s in sessions if s.call_count > 0]

        return {
            "generated_at": time.time(),
            "active_sessions": len(active),
            "total_tokens_all_sessions": sum(s.total_tokens for s in active),
            "avg_tokens_per_session": (
                round(sum(s.total_tokens for s in active) / len(active))
                if active else 0
            ),
            "top_burning_sessions": sorted(
                [{"session_id": s.session_id, "burn_rate": s.burn_rate_tokens_per_minute()}
                 for s in active],
                key=lambda x: -x["burn_rate"],
            )[:5],
            "top_users_24h": self._ledger.top_consumers(top_n=5),
            "recent_alerts": [
                {"session_id": a.session_id, "utilization": a.utilization, "msg": a.message}
                for a in self._alerts.recent_alerts(3600.0)
            ],
        }
```

## Comparison

| Approach | Per-Call Tracking | Session Accumulation | User Daily Ledger | Budget Alerts | Dashboard |
|---|---|---|---|---|---|
| TokenUsageRecord | Yes (dataclass) | No | No | No | No |
| SessionTokenAccumulator | Via records | Yes | No | No | No |
| UserDailyTokenLedger | Via records | No | Yes (rolling 24h) | No | No |
| TokenBudgetAlertManager | No | Via accumulator | No | Yes | No |
| LLMCallTokenInterceptor | Yes (intercepts) | Via accumulator | Via ledger | Via alerts | No |
| TokenBudgetUtilizationDashboard | No | No | No | No | Yes |

**Best for production**: Intercept at the LLM call level rather than instrumenting each call site — a single `LLMCallTokenInterceptor` wrapper ensures no call escapes measurement. Track `cached_tokens` separately from billed tokens: prompt cache hits cost ~10% of full input tokens on Anthropic and $0 on some providers, so conflating them inflates cost estimates. Alert at 80% session budget utilization with burn rate context — "consuming 12,000 tokens/minute, projected to exhaust in 8 minutes" is actionable; "80% used" alone is not. Set user daily budgets per tier and enforce them before dispatching the LLM call, not after — post-call enforcement still incurs cost.
