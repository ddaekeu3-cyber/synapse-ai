---
title: "Agent Doesn't Implement Token Count Estimation Before API Submission"
description: "AI agents that assemble context and tool results without checking token counts submit requests that silently truncate, exceed context windows, or incur unexpected costs. Pre-submission token estimation detects overflow early, triggers context trimming strategies, and records per-request token budgets so the agent never discovers a context-window violation from a 400 error mid-conversation."
date: 2025-02-17
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-token-count-estimation-before-api-submission
tags:
  - token-counting
  - context-window
  - cost-control
  - estimation
  - performance
  - truncation
  - budget
symptoms:
  - "API returns 400 context_length_exceeded after context grows past 200k tokens"
  - "Long tool results are silently truncated by the API without agent awareness"
  - "No per-request token count is logged, making cost attribution impossible"
  - "Agent appends all tool results without checking if they fit in remaining budget"
  - "System prompt + history + tool results exceed model limit mid-conversation"
---

## Problem

Context window overflow is discovered too late — at the API call site — when it produces a 400 error or silent truncation. Pre-submission estimation uses tiktoken (OpenAI) or the Anthropic tokenizer heuristic (≈4 chars/token) to count tokens before the request is sent. If the estimate exceeds a budget threshold, the agent applies a trimming strategy — drop oldest messages, summarize tool results, or truncate to fit — before submission. This converts a runtime error into a controlled, predictable behavior with no user-visible failure.

---

## Solution 1: TokenEstimator — Fast Pre-Submission Token Count

```python
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union


@dataclass
class TokenEstimate:
    total_tokens: int
    system_tokens: int
    message_tokens: int
    overhead_tokens: int   # Per-message formatting overhead
    budget_remaining: int
    over_budget: bool

    def utilization(self) -> float:
        if self.total_tokens == 0:
            return 0.0
        return round(self.total_tokens / (self.total_tokens + self.budget_remaining), 3)


class TokenEstimator:
    """
    Estimates token counts for Anthropic API requests without calling
    the tokenizer (which requires a network round-trip). Uses the
    ~4 chars/token heuristic for prose and ~3 chars/token for code.
    Accurate to ±10%, sufficient for overflow detection.

    Usage:
        estimator = TokenEstimator(model_context_limit=200_000)
        estimate = estimator.estimate(
            system="You are a helpful assistant.",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens_to_sample=1024,
        )
        if estimate.over_budget:
            trim_context(messages)
    """

    # Per-model context window limits (input tokens)
    MODEL_LIMITS = {
        "claude-opus-4-6": 200_000,
        "claude-sonnet-4-6": 200_000,
        "claude-haiku-4-5-20251001": 200_000,
    }

    # Conservative chars-per-token estimate
    CHARS_PER_TOKEN_PROSE = 3.5
    CHARS_PER_TOKEN_CODE = 3.0
    # Per-message overhead (role, formatting tokens)
    MESSAGE_OVERHEAD = 5

    def __init__(self, model: str = "claude-sonnet-4-6",
                  safety_margin: float = 0.05):
        self._limit = self.MODEL_LIMITS.get(model, 200_000)
        self._margin = safety_margin

    def _chars_to_tokens(self, text: str) -> int:
        if not text:
            return 0
        # Use lower CPT for content with lots of code/symbols
        code_fraction = len(re.findall(r'[{}()\[\]<>;=]', text)) / max(len(text), 1)
        cpt = self.CHARS_PER_TOKEN_CODE if code_fraction > 0.05 else self.CHARS_PER_TOKEN_PROSE
        return max(1, int(len(text) / cpt))

    def _content_tokens(self, content: Union[str, list]) -> int:
        if isinstance(content, str):
            return self._chars_to_tokens(content)
        if isinstance(content, list):
            total = 0
            for block in content:
                if isinstance(block, dict):
                    total += self._chars_to_tokens(block.get("text", ""))
            return total
        return 0

    def estimate(self, messages: List[Dict[str, Any]],
                  system: str = "",
                  max_tokens_to_sample: int = 1024) -> TokenEstimate:
        system_tokens = self._chars_to_tokens(system)
        message_tokens = 0
        for msg in messages:
            message_tokens += self._content_tokens(msg.get("content", ""))
            message_tokens += self.MESSAGE_OVERHEAD

        overhead = 10  # conversation-level formatting
        total = system_tokens + message_tokens + overhead
        effective_limit = int(self._limit * (1 - self._margin)) - max_tokens_to_sample
        remaining = max(0, effective_limit - total)

        return TokenEstimate(
            total_tokens=total,
            system_tokens=system_tokens,
            message_tokens=message_tokens,
            overhead_tokens=overhead,
            budget_remaining=remaining,
            over_budget=total > effective_limit,
        )

    def fits(self, messages: List[Dict[str, Any]],
              system: str = "",
              max_tokens: int = 1024) -> bool:
        return not self.estimate(messages, system, max_tokens).over_budget
```

---

## Solution 2: MessageTrimmer — Reduce Context to Fit Budget

```python
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class MessageTrimmer:
    """
    Trims a message list to fit within a token budget using configurable
    strategies: drop oldest non-system messages, truncate long tool results,
    or summarize runs of assistant/tool messages.

    Usage:
        trimmer = MessageTrimmer(estimator)
        trimmed, removed = trimmer.trim_to_fit(
            messages,
            system=system_prompt,
            max_tokens=1024,
        )
        assert estimator.fits(trimmed, system_prompt, max_tokens)
    """

    def __init__(self, estimator: TokenEstimator,
                  min_messages: int = 2,
                  tool_result_max_chars: int = 2_000):
        self._est = estimator
        self._min = min_messages
        self._tool_max = tool_result_max_chars

    def trim_to_fit(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        max_tokens: int = 1024,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Returns (trimmed_messages, num_removed)."""
        msgs = list(messages)
        removed = 0

        # Step 1: Truncate oversized tool results
        msgs = self._truncate_tool_results(msgs)

        # Step 2: Drop oldest non-pinned messages until fits
        while (not self._est.fits(msgs, system, max_tokens)
               and len(msgs) > self._min):
            # Find oldest droppable message (not the last user message)
            for i, msg in enumerate(msgs[:-1]):
                if msg.get("role") in ("user", "assistant", "tool"):
                    msgs.pop(i)
                    removed += 1
                    break
            else:
                break  # Nothing left to drop

        if not self._est.fits(msgs, system, max_tokens):
            logger.warning(
                "context_trim_insufficient removed=%d still_over_budget",
                removed,
            )
        else:
            logger.info(
                "context_trimmed removed=%d remaining=%d",
                removed, len(msgs),
            )

        return msgs, removed

    def _truncate_tool_results(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Truncate tool result content that exceeds max chars."""
        result = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self._tool_max:
                    truncated = content[:self._tool_max]
                    msg = {**msg, "content": truncated + "\n[truncated]"}
                    logger.debug(
                        "tool_result_truncated original=%d max=%d",
                        len(content), self._tool_max,
                    )
            result.append(msg)
        return result
```

---

## Solution 3: TokenBudgetAllocator — Partition Budget Across Components

```python
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class BudgetAllocation:
    system_budget: int
    history_budget: int
    tool_results_budget: int
    completion_budget: int
    total_limit: int

    def remaining_for_new_content(self) -> int:
        used = self.system_budget + self.history_budget + self.tool_results_budget
        return max(0, self.total_limit - used - self.completion_budget)


class TokenBudgetAllocator:
    """
    Partitions the model's context window across fixed components so each
    component has a defined allocation. Prevents any single component from
    crowding out others — e.g., a large tool result consuming history space.

    Usage:
        allocator = TokenBudgetAllocator(
            total_limit=200_000,
            system_fraction=0.05,
            history_fraction=0.40,
            tool_results_fraction=0.35,
            completion_tokens=4096,
        )
        alloc = allocator.allocate()
        # Trim system prompt to alloc.system_budget tokens
        # Trim history to alloc.history_budget tokens
    """

    def __init__(self, total_limit: int = 200_000,
                  system_fraction: float = 0.05,
                  history_fraction: float = 0.40,
                  tool_results_fraction: float = 0.35,
                  completion_tokens: int = 4096):
        self._total = total_limit
        self._sys_frac = system_fraction
        self._hist_frac = history_fraction
        self._tool_frac = tool_results_fraction
        self._completion = completion_tokens

    def allocate(self) -> BudgetAllocation:
        usable = self._total - self._completion
        system = int(usable * self._sys_frac)
        history = int(usable * self._hist_frac)
        tools = int(usable * self._tool_frac)
        # Remainder goes to miscellaneous/overflow buffer
        return BudgetAllocation(
            system_budget=system,
            history_budget=history,
            tool_results_budget=tools,
            completion_budget=self._completion,
            total_limit=self._total,
        )

    def fits_system(self, system_tokens: int) -> bool:
        return system_tokens <= self.allocate().system_budget

    def fits_history(self, history_tokens: int) -> bool:
        return history_tokens <= self.allocate().history_budget
```

---

## Solution 4: ContextWindowMonitor — Track and Alert on Token Growth

```python
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TurnTokenRecord:
    turn: int
    message_tokens: int
    system_tokens: int
    total_tokens: int
    utilization: float
    timestamp: float


class ContextWindowMonitor:
    """
    Tracks token utilization across conversation turns and warns when
    growth rate predicts overflow within the next N turns. Surfaces
    the growth trend so the agent can proactively compress or summarize.

    Usage:
        monitor = ContextWindowMonitor(model="claude-sonnet-4-6")

        for turn, (messages, system) in enumerate(conversation):
            record = monitor.record_turn(turn, messages, system)
            if monitor.predicts_overflow_within(turns=3):
                compress_oldest_messages(messages)
    """

    def __init__(self, model: str = "claude-sonnet-4-6",
                  history_turns: int = 20):
        self._estimator = TokenEstimator(model=model)
        self._records: Deque[TurnTokenRecord] = deque(maxlen=history_turns)

    def record_turn(self, turn: int,
                     messages: List[Dict[str, Any]],
                     system: str = "",
                     max_tokens: int = 1024) -> TurnTokenRecord:
        est = self._estimator.estimate(messages, system, max_tokens)
        record = TurnTokenRecord(
            turn=turn,
            message_tokens=est.message_tokens,
            system_tokens=est.system_tokens,
            total_tokens=est.total_tokens,
            utilization=est.utilization(),
            timestamp=time.monotonic(),
        )
        self._records.append(record)
        if record.utilization > 0.85:
            logger.warning(
                "context_window_high_utilization turn=%d utilization=%.1f%%",
                turn, record.utilization * 100,
            )
        return record

    def growth_rate_per_turn(self) -> float:
        """Tokens added per turn (linear estimate)."""
        recs = list(self._records)
        if len(recs) < 2:
            return 0.0
        deltas = [
            recs[i].total_tokens - recs[i - 1].total_tokens
            for i in range(1, len(recs))
        ]
        return sum(deltas) / len(deltas)

    def predicts_overflow_within(self, turns: int = 5,
                                   model: str = "claude-sonnet-4-6") -> bool:
        if not self._records:
            return False
        limit = TokenEstimator.MODEL_LIMITS.get(model, 200_000)
        current = self._records[-1].total_tokens
        rate = self.growth_rate_per_turn()
        projected = current + rate * turns
        return projected > limit * 0.90

    def summary(self) -> Dict[str, Any]:
        if not self._records:
            return {}
        latest = self._records[-1]
        return {
            "current_tokens": latest.total_tokens,
            "utilization_pct": round(latest.utilization * 100, 1),
            "growth_per_turn": round(self.growth_rate_per_turn(), 0),
            "overflow_risk_5_turns": self.predicts_overflow_within(5),
        }
```

---

## Solution 5: PreSubmissionTokenGuard — Enforce Budget at Call Site

```python
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class PreSubmissionTokenGuard:
    """
    Wraps the LLM API call to enforce token budget checks before submission.
    If the estimated token count exceeds the threshold, applies automatic
    trimming. Records per-request token stats for cost attribution.

    Usage:
        guard = PreSubmissionTokenGuard(
            llm_fn=anthropic_client.messages.create,
            model="claude-sonnet-4-6",
            auto_trim=True,
        )
        response = await guard.call(
            messages=messages,
            system=system,
            max_tokens=1024,
        )
    """

    def __init__(self, llm_fn: Callable,
                  model: str = "claude-sonnet-4-6",
                  auto_trim: bool = True,
                  hard_reject: bool = False):
        self._fn = llm_fn
        self._model = model
        self._auto_trim = auto_trim
        self._hard_reject = hard_reject
        self._estimator = TokenEstimator(model=model)
        self._trimmer = MessageTrimmer(self._estimator)
        self._request_count = 0
        self._trim_count = 0

    async def call(self, messages: List[Dict[str, Any]],
                    system: str = "",
                    max_tokens: int = 1024,
                    **kwargs) -> Any:
        self._request_count += 1
        estimate = self._estimator.estimate(messages, system, max_tokens)

        logger.debug(
            "pre_submission_check tokens=%d utilization=%.1f%% over=%s",
            estimate.total_tokens, estimate.utilization() * 100,
            estimate.over_budget,
        )

        if estimate.over_budget:
            if self._hard_reject:
                raise ValueError(
                    f"Request exceeds token budget: {estimate.total_tokens} tokens"
                )
            if self._auto_trim:
                messages, removed = self._trimmer.trim_to_fit(
                    messages, system, max_tokens
                )
                self._trim_count += 1
                logger.info(
                    "auto_trimmed removed=%d before submission", removed
                )

        return await self._fn(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            model=self._model,
            **kwargs,
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "total_requests": self._request_count,
            "auto_trim_count": self._trim_count,
            "trim_rate": round(
                self._trim_count / max(self._request_count, 1), 3
            ),
        }
```

---

## Solution 6: TokenAwareAgentLoop — Full Budget-Conscious Conversation Loop

```python
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TokenAwareAgentLoop:
    """
    Conversation loop that estimates tokens before every LLM call,
    allocates budget across components, and automatically compresses
    context when growth threatens the window limit.

    Usage:
        loop = TokenAwareAgentLoop(llm_fn=client.messages.create)
        response = await loop.turn(
            user_message="Summarize the document",
            tool_results=[{"role": "tool", "content": long_doc}],
        )
    """

    def __init__(self, llm_fn,
                  system: str = "",
                  model: str = "claude-sonnet-4-6",
                  max_tokens: int = 4096):
        self._system = system
        self._model = model
        self._max_tokens = max_tokens
        self._messages: List[Dict[str, Any]] = []
        self._estimator = TokenEstimator(model=model)
        self._allocator = TokenBudgetAllocator(
            total_limit=TokenEstimator.MODEL_LIMITS.get(model, 200_000),
            completion_tokens=max_tokens,
        )
        self._monitor = ContextWindowMonitor(model=model)
        self._guard = PreSubmissionTokenGuard(
            llm_fn=llm_fn, model=model, auto_trim=True
        )
        self._turn_count = 0

    async def turn(self, user_message: str,
                    tool_results: Optional[List[Dict[str, Any]]] = None) -> str:
        self._messages.append({"role": "user", "content": user_message})
        if tool_results:
            self._messages.extend(tool_results)

        # Monitor growth and log warnings
        record = self._monitor.record_turn(
            self._turn_count, self._messages, self._system, self._max_tokens
        )
        self._turn_count += 1

        if self._monitor.predicts_overflow_within(turns=3):
            logger.warning(
                "context_overflow_predicted turn=%d current=%d "
                "growth_per_turn=%.0f",
                self._turn_count - 1,
                record.total_tokens,
                self._monitor.growth_rate_per_turn(),
            )

        response = await self._guard.call(
            messages=self._messages,
            system=self._system,
            max_tokens=self._max_tokens,
        )

        assistant_text = (
            response.content[0].text
            if hasattr(response, "content")
            else str(response)
        )
        self._messages.append({"role": "assistant", "content": assistant_text})
        return assistant_text

    def context_report(self) -> Dict[str, Any]:
        return {
            "turns": self._turn_count,
            "monitor": self._monitor.summary(),
            "guard_stats": self._guard.stats(),
        }
```

---

## Comparison

| Approach | Estimation | Trimming | Budget Allocation | Growth Monitoring | Auto-Enforce | Integrated |
|---|---|---|---|---|---|---|
| **TokenEstimator** | Yes | No | No | No | No | No |
| **MessageTrimmer** | Via estimator | Yes | No | No | No | No |
| **TokenBudgetAllocator** | No | No | Yes | No | No | No |
| **ContextWindowMonitor** | Yes | No | No | Yes | No | No |
| **PreSubmissionTokenGuard** | Yes | Yes | No | No | Yes | No |
| **TokenAwareAgentLoop** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: the ±10% estimation error of the 4-char/token heuristic is acceptable because the guard triggers trimming at 95% utilization, leaving a 5% buffer that absorbs estimation error. Never wait for a 400 `context_length_exceeded` — estimate before every API call. Track growth rate per turn: a conversation that adds 2,000 tokens per turn will overflow a 200k-token window after 100 turns, but monitoring detects this at turn 85 and triggers compression while there is still room to work with.
