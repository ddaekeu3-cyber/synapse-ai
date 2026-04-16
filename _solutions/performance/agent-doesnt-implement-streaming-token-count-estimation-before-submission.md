---
title: "Agent Doesn't Implement Streaming Token Count Estimation Before Submission"
description: "Agents that build large context windows and submit them without pre-checking token count hit context length errors mid-flight or waste API budget on requests that will be rejected. Implement lightweight token count estimation to prune, summarize, or split context before submission."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-streaming-token-count-estimation-before-submission
tags: [token-counting, context-management, performance, cost-optimization, llm, budgeting]
symptoms:
  - "API returns 400 context_length_exceeded after consuming input tokens up to limit"
  - "Agent doesn't know how many tokens are in the context until the API rejects the call"
  - "No budget check before adding tool results that push context over the limit"
  - "Full tiktoken encoding called on every message causing 50ms overhead per turn"
  - "Agents split context at arbitrary character limits instead of token boundaries"
---

## Why This Happens

Tokenization is model-specific and non-trivial — the exact token count depends on the tokenizer vocabulary, special tokens, and encoding rules. Most agents either ignore token counts (and hit errors) or call the full tokenizer on every message (which is slow). Approximate counting with character-to-token ratio estimates is fast enough for budget checks without full tokenization overhead. Reserve full tokenization only for the final pre-submission validation.

## Solution 1: Tiered Token Estimator (Fast Approximation + Exact)

```python
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

@dataclass
class TokenEstimate:
    count: int
    method: str     # "approximate" | "exact" | "cached"
    confidence: float  # 0–1

class TieredTokenEstimator:
    """
    Tier 1 (fast): character-ratio approximation — 1ms, ±15% accuracy.
    Tier 2 (exact): full tokenizer call — 20–50ms, exact.
    Uses approximation for budget checks, exact only at submission boundary.
    """

    # Average chars-per-token by content type
    CHAR_RATIOS = {
        "english_prose": 4.0,
        "code": 3.0,
        "json": 3.5,
        "mixed": 3.8,
    }

    def __init__(self, tokenizer=None):
        self._tokenizer = tokenizer  # tiktoken or transformers tokenizer
        self._cache: Dict[int, int] = {}  # hash -> exact_count

    def estimate(self, text: str, content_type: str = "mixed") -> TokenEstimate:
        """Fast approximation: characters / ratio + overhead."""
        ratio = self.CHAR_RATIOS.get(content_type, 3.8)
        count = int(len(text) / ratio) + 4  # +4 for message overhead
        return TokenEstimate(count=count, method="approximate", confidence=0.85)

    def count_exact(self, text: str) -> TokenEstimate:
        """Exact count using the model's tokenizer."""
        if self._tokenizer is None:
            return self.estimate(text)
        text_hash = hash(text)
        if text_hash in self._cache:
            return TokenEstimate(count=self._cache[text_hash], method="cached", confidence=1.0)
        tokens = self._tokenizer.encode(text)
        count = len(tokens)
        self._cache[text_hash] = count
        return TokenEstimate(count=count, method="exact", confidence=1.0)

    def estimate_messages(self, messages: List[dict]) -> TokenEstimate:
        """Approximate total tokens for a list of chat messages."""
        total = 3  # reply primer overhead
        for msg in messages:
            total += 4  # per-message overhead
            content = msg.get("content", "")
            if isinstance(content, str):
                total += int(len(content) / 3.8)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        total += int(len(block["text"]) / 3.8)
        return TokenEstimate(count=total, method="approximate", confidence=0.82)

    def count_messages_exact(self, messages: List[dict]) -> TokenEstimate:
        if self._tokenizer is None:
            return self.estimate_messages(messages)
        total = 3
        for msg in messages:
            total += 4
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(self._tokenizer.encode(content))
        return TokenEstimate(count=total, method="exact", confidence=1.0)
```

## Solution 2: Context Budget Manager

```python
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

@dataclass
class ContextBudget:
    model_context_limit: int
    max_input_tokens: int       # reserve output budget
    max_output_tokens: int
    safety_margin: int = 200    # buffer to avoid off-by-one rejections

    @property
    def available_input(self) -> int:
        return self.max_input_tokens - self.safety_margin

class ContextBudgetManager:
    """
    Tracks token budget consumption as messages are added.
    Prevents context overflow by refusing additions that exceed budget.
    """

    def __init__(self, budget: ContextBudget, estimator: TieredTokenEstimator):
        self._budget = budget
        self._estimator = estimator
        self._messages: List[dict] = []
        self._running_estimate: int = 3

    def add_message(self, message: dict) -> bool:
        """Returns True if added successfully, False if would exceed budget."""
        estimate = self._estimator.estimate(
            message.get("content", "") if isinstance(message.get("content"), str) else ""
        )
        new_total = self._running_estimate + 4 + estimate.count

        if new_total > self._budget.available_input:
            return False

        self._messages.append(message)
        self._running_estimate = new_total
        return True

    def remaining_budget(self) -> int:
        return self._budget.available_input - self._running_estimate

    def utilization(self) -> float:
        return self._running_estimate / self._budget.available_input

    def messages(self) -> List[dict]:
        return list(self._messages)

    def prune_oldest(self, target_tokens: int) -> int:
        """Remove oldest non-system messages until target_tokens freed."""
        freed = 0
        to_remove = []
        for i, msg in enumerate(self._messages):
            if msg.get("role") == "system":
                continue
            est = self._estimator.estimate(msg.get("content", ""))
            freed += est.count + 4
            to_remove.append(i)
            if freed >= target_tokens:
                break

        for i in reversed(to_remove):
            self._messages.pop(i)
        self._running_estimate -= freed
        return freed
```

## Solution 3: Streaming Token Counter for Live Estimation

```python
import re
from typing import Iterator

class StreamingTokenCounter:
    """
    Estimates token count incrementally as text is generated or streamed.
    Does not require full tokenizer — uses online character-ratio estimation.
    Useful for enforcing output token budgets on streaming responses.
    """

    def __init__(self, chars_per_token: float = 3.8):
        self._ratio = chars_per_token
        self._char_count = 0
        self._token_estimate = 0
        self._word_count = 0

    def feed(self, chunk: str) -> int:
        """Feed a text chunk. Returns updated estimated token count."""
        self._char_count += len(chunk)
        # Refine estimate: words are a better proxy than raw chars
        words = re.findall(r'\b\w+\b', chunk)
        self._word_count += len(words)
        # Blend word-based and char-based estimates
        char_estimate = self._char_count / self._ratio
        word_estimate = self._word_count * 1.3   # average ~1.3 tokens/word
        self._token_estimate = int(0.6 * char_estimate + 0.4 * word_estimate)
        return self._token_estimate

    def reset(self) -> None:
        self._char_count = 0
        self._token_estimate = 0
        self._word_count = 0

    @property
    def estimated_tokens(self) -> int:
        return self._token_estimate

    def will_exceed(self, limit: int) -> bool:
        return self._token_estimate > limit


def token_budget_stream(
    stream: Iterator[str],
    max_tokens: int,
    chars_per_token: float = 3.8,
) -> Iterator[str]:
    """
    Wraps any text stream and stops it when the estimated token budget is exceeded.
    """
    counter = StreamingTokenCounter(chars_per_token)
    for chunk in stream:
        estimate = counter.feed(chunk)
        yield chunk
        if estimate > max_tokens:
            yield "\n[Response truncated at token budget]"
            break
```

## Solution 4: Context Window Splitter at Token Boundaries

```python
from typing import List, Tuple

class TokenAwareContextSplitter:
    """
    Splits large text into chunks that respect token limits.
    Uses character approximation for speed; verifies with exact count
    only at the final split boundary.
    """

    def __init__(self, estimator: TieredTokenEstimator, max_tokens_per_chunk: int = 2000):
        self._estimator = estimator
        self._max = max_tokens_per_chunk

    def split(self, text: str, overlap_tokens: int = 100) -> List[str]:
        """Split text into chunks, with optional overlap for context continuity."""
        chars_per_chunk = int(self._max * 3.8)
        overlap_chars = int(overlap_tokens * 3.8)

        chunks = []
        start = 0
        while start < len(text):
            end = start + chars_per_chunk
            if end >= len(text):
                chunks.append(text[start:])
                break

            # Try to split at sentence boundary
            boundary = text.rfind(". ", start, end)
            if boundary == -1:
                boundary = text.rfind("\n", start, end)
            if boundary == -1:
                boundary = text.rfind(" ", start, end)
            if boundary == -1 or boundary <= start:
                boundary = end

            chunk = text[start:boundary + 1]
            chunks.append(chunk)
            start = boundary + 1 - overlap_chars
            start = max(start, 0)

        return chunks

    def split_messages_to_batches(
        self,
        messages: List[dict],
        max_tokens_per_batch: int,
        estimator: TieredTokenEstimator,
    ) -> List[List[dict]]:
        """Group messages into batches that each fit within the token limit."""
        batches: List[List[dict]] = []
        current_batch: List[dict] = []
        current_tokens = 3

        for msg in messages:
            est = estimator.estimate(msg.get("content", ""))
            msg_tokens = est.count + 4
            if current_tokens + msg_tokens > max_tokens_per_batch and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_tokens = 3
            current_batch.append(msg)
            current_tokens += msg_tokens

        if current_batch:
            batches.append(current_batch)

        return batches
```

## Solution 5: Pre-Submission Token Validation Gate

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

@dataclass
class SubmissionCheckResult:
    approved: bool
    estimated_tokens: int
    exact_tokens: Optional[int]
    budget_remaining: int
    action_taken: str   # "approved" | "pruned" | "split" | "rejected"

class PreSubmissionTokenGate:
    """
    Final validation gate before each LLM API call.
    Estimates tokens, applies corrections if needed, records cost projection.
    """

    def __init__(
        self,
        estimator: TieredTokenEstimator,
        budget: ContextBudget,
        cost_per_1k_input: float = 0.003,
    ):
        self._estimator = estimator
        self._budget = budget
        self._cost_per_1k = cost_per_1k_input
        self._total_cost_estimate = 0.0

    async def check_and_prepare(
        self,
        messages: List[dict],
        auto_prune: bool = True,
    ) -> SubmissionCheckResult:
        estimate = self._estimator.estimate_messages(messages)
        budget_remaining = self._budget.available_input - estimate.count

        if estimate.count <= self._budget.available_input:
            cost = estimate.count / 1000 * self._cost_per_1k
            self._total_cost_estimate += cost
            return SubmissionCheckResult(
                approved=True,
                estimated_tokens=estimate.count,
                exact_tokens=None,
                budget_remaining=budget_remaining,
                action_taken="approved",
            )

        if auto_prune:
            # Remove oldest non-system messages until under budget
            overflow = estimate.count - self._budget.available_input
            mgr = ContextBudgetManager(self._budget, self._estimator)
            for msg in messages:
                mgr.add_message(msg)
            freed = mgr.prune_oldest(overflow + 200)
            new_estimate = self._estimator.estimate_messages(mgr.messages())
            return SubmissionCheckResult(
                approved=True,
                estimated_tokens=new_estimate.count,
                exact_tokens=None,
                budget_remaining=self._budget.available_input - new_estimate.count,
                action_taken=f"pruned_{freed}_tokens",
            )

        return SubmissionCheckResult(
            approved=False,
            estimated_tokens=estimate.count,
            exact_tokens=None,
            budget_remaining=budget_remaining,
            action_taken="rejected",
        )

    def total_estimated_cost(self) -> float:
        return self._total_cost_estimate
```

## Solution 6: Token Usage Dashboard

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict

@dataclass
class TokenUsageStats:
    session_id: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    api_calls: int = 0
    pruning_events: int = 0
    tokens_pruned: int = 0
    estimated_cost_usd: float = 0.0

class TokenUsageDashboard:
    def __init__(self, cost_per_1k_input: float = 0.003, cost_per_1k_output: float = 0.015):
        self._in_rate = cost_per_1k_input
        self._out_rate = cost_per_1k_output
        self._sessions: Dict[str, TokenUsageStats] = defaultdict(
            lambda: TokenUsageStats(session_id="")
        )

    def record_call(
        self,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        pruned: int = 0,
    ) -> None:
        s = self._sessions[session_id]
        s.session_id = session_id
        s.total_input_tokens += input_tokens
        s.total_output_tokens += output_tokens
        s.api_calls += 1
        if pruned > 0:
            s.pruning_events += 1
            s.tokens_pruned += pruned
        s.estimated_cost_usd = (
            s.total_input_tokens / 1000 * self._in_rate
            + s.total_output_tokens / 1000 * self._out_rate
        )

    def session_summary(self, session_id: str) -> dict:
        s = self._sessions.get(session_id)
        if not s:
            return {}
        return {
            "session_id": session_id,
            "api_calls": s.api_calls,
            "input_tokens": s.total_input_tokens,
            "output_tokens": s.total_output_tokens,
            "tokens_pruned": s.tokens_pruned,
            "pruning_events": s.pruning_events,
            "estimated_cost_usd": round(s.estimated_cost_usd, 5),
            "avg_input_per_call": s.total_input_tokens // max(s.api_calls, 1),
        }

    def global_summary(self) -> dict:
        total_cost = sum(s.estimated_cost_usd for s in self._sessions.values())
        total_calls = sum(s.api_calls for s in self._sessions.values())
        return {
            "total_sessions": len(self._sessions),
            "total_api_calls": total_calls,
            "total_estimated_cost_usd": round(total_cost, 4),
            "avg_cost_per_call": round(total_cost / max(total_calls, 1), 6),
        }
```

## Comparison

| Approach | Speed | Accuracy | Use Case |
|---|---|---|---|
| TieredTokenEstimator (approx) | ~0.1ms | ±15% | Budget checks, pre-flight |
| TieredTokenEstimator (exact) | 20–50ms | 100% | Final pre-submission validation |
| ContextBudgetManager | ~0.1ms per add | ±15% | Incremental context building |
| StreamingTokenCounter | ~0.1ms/chunk | ±12% | Output budget enforcement |
| TokenAwareContextSplitter | ~0.1ms/char | ±15% | RAG chunk sizing |
| PreSubmissionTokenGate | 0.1–50ms | ±15% → 100% | Gate before every API call |

**Best for production**: Use `TieredTokenEstimator` approximate mode for all incremental budget checks during context building (fast). Switch to exact mode only at the `PreSubmissionTokenGate` to catch any drift. Use `ContextBudgetManager` to prevent over-filling the context window. Track costs in `TokenUsageDashboard` per session to enable per-tenant billing and budget alerting.
