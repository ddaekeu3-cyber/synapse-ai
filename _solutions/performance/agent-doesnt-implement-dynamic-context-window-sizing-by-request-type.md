---
title: "Agent Doesn't Implement Dynamic Context Window Sizing by Request Type"
description: "Agents that use a fixed context window for every request waste tokens on simple queries and truncate necessary context on complex ones. A lookup question needs 2K tokens; a multi-document synthesis needs 128K. Implement dynamic context window sizing that classifies request complexity and adjusts the effective context budget before tool calls and LLM invocations, reducing cost on simple requests without sacrificing quality on complex ones."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-dynamic-context-window-sizing-by-request-type
tags: [context-window, token-budget, request-classification, dynamic-sizing, cost-optimization, llm-efficiency]
symptoms:
  - "Simple lookup requests consume the same token budget as complex multi-document synthesis"
  - "Fixed context limit causes truncation on long reasoning chains but wastes budget on short ones"
  - "No mechanism to scale retrieval depth or history inclusion based on request complexity"
  - "Cost per request is uniform regardless of actual complexity"
  - "Context window is always filled to the same ceiling regardless of what the task needs"
---

## Why This Happens

Most agent frameworks initialize a single context window size at startup and use it unchanged for every request. This creates a bimodal waste pattern: simple requests (factual lookups, yes/no decisions, single-tool calls) use a fraction of the allocated window but are billed for the entire context; complex requests (multi-step reasoning, large document synthesis, long conversation continuations) hit the fixed ceiling and truncate context that matters. Dynamic sizing requires classifying the request before the LLM is invoked, mapping complexity to a token budget, and communicating that budget to retrieval, history injection, and tool result formatting components.

## Solution 1: Request Complexity Classifier

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List


class RequestComplexity(str, Enum):
    TRIVIAL = "trivial"         # single-fact lookup, yes/no, short answer
    SIMPLE = "simple"           # single-tool call, short synthesis
    MODERATE = "moderate"       # multi-tool, paragraph-length answer
    COMPLEX = "complex"         # multi-document, chain-of-thought
    EXHAUSTIVE = "exhaustive"   # full corpus synthesis, long-form generation


@dataclass
class ComplexitySignal:
    name: str
    weight: float
    detected: bool = False


class RequestComplexityClassifier:
    """
    Classifies a user request into a complexity tier based on lexical
    signals: question type, document count hints, requested output length,
    multi-step connectives, and prior conversation depth.
    """

    _TRIVIAL_PATTERNS = [
        r"\bwhat is\b", r"\bwho is\b", r"\bwhen did\b", r"\byes or no\b",
        r"\bdefine\b", r"\bhow many\b",
    ]
    _COMPLEX_PATTERNS = [
        r"\bcompare\b", r"\bcontrast\b", r"\banalyze\b", r"\bsummarize all\b",
        r"\bwrite a report\b", r"\bstep by step\b", r"\bchain of thought\b",
        r"\bacross all\b", r"\bevery document\b",
    ]
    _MULTI_TOOL_PATTERNS = [
        r"\band then\b", r"\bafter that\b", r"\bfirst .{0,30} then\b",
        r"\bmultiple sources\b", r"\bseveral\b",
    ]

    def classify(
        self,
        user_message: str,
        conversation_turns: int = 0,
        attached_document_count: int = 0,
    ) -> RequestComplexity:
        text = user_message.lower()

        trivial_hits = sum(
            1 for p in self._TRIVIAL_PATTERNS if re.search(p, text)
        )
        complex_hits = sum(
            1 for p in self._COMPLEX_PATTERNS if re.search(p, text)
        )
        multi_tool_hits = sum(
            1 for p in self._MULTI_TOOL_PATTERNS if re.search(p, text)
        )

        score = 0
        score += complex_hits * 3
        score += multi_tool_hits * 2
        score += attached_document_count * 2
        score += min(conversation_turns // 5, 4)
        score -= trivial_hits * 2

        if score <= 0:
            return RequestComplexity.TRIVIAL
        if score <= 2:
            return RequestComplexity.SIMPLE
        if score <= 5:
            return RequestComplexity.MODERATE
        if score <= 9:
            return RequestComplexity.COMPLEX
        return RequestComplexity.EXHAUSTIVE
```

## Solution 2: Context Budget Policy

```python
from dataclasses import dataclass
from typing import Dict


@dataclass
class ContextBudget:
    total_tokens: int
    system_prompt_tokens: int
    conversation_history_tokens: int
    tool_result_tokens: int
    retrieved_context_tokens: int
    generation_reserve_tokens: int   # reserved for the model's output

    def available_for_content(self) -> int:
        return (
            self.total_tokens
            - self.system_prompt_tokens
            - self.generation_reserve_tokens
        )


# Budgets tuned for typical 128K-window models
_BUDGET_TABLE: Dict[RequestComplexity, ContextBudget] = {
    RequestComplexity.TRIVIAL: ContextBudget(
        total_tokens=4096,
        system_prompt_tokens=512,
        conversation_history_tokens=512,
        tool_result_tokens=1024,
        retrieved_context_tokens=1024,
        generation_reserve_tokens=1024,
    ),
    RequestComplexity.SIMPLE: ContextBudget(
        total_tokens=8192,
        system_prompt_tokens=512,
        conversation_history_tokens=1024,
        tool_result_tokens=2048,
        retrieved_context_tokens=2048,
        generation_reserve_tokens=2048,
    ),
    RequestComplexity.MODERATE: ContextBudget(
        total_tokens=32768,
        system_prompt_tokens=1024,
        conversation_history_tokens=4096,
        tool_result_tokens=8192,
        retrieved_context_tokens=12288,
        generation_reserve_tokens=4096,
    ),
    RequestComplexity.COMPLEX: ContextBudget(
        total_tokens=65536,
        system_prompt_tokens=2048,
        conversation_history_tokens=8192,
        tool_result_tokens=16384,
        retrieved_context_tokens=28672,
        generation_reserve_tokens=8192,
    ),
    RequestComplexity.EXHAUSTIVE: ContextBudget(
        total_tokens=131072,
        system_prompt_tokens=4096,
        conversation_history_tokens=16384,
        tool_result_tokens=32768,
        retrieved_context_tokens=65536,
        generation_reserve_tokens=12288,
    ),
}


class ContextBudgetPolicy:
    def get(self, complexity: RequestComplexity) -> ContextBudget:
        return _BUDGET_TABLE[complexity]

    def override(
        self, complexity: RequestComplexity, budget: ContextBudget
    ) -> None:
        _BUDGET_TABLE[complexity] = budget
```

## Solution 3: History Trimmer

```python
from typing import List, Tuple


@dataclass
class ConversationTurn:
    role: str   # "user" | "assistant"
    content: str
    token_count: int


class BudgetedHistoryTrimmer:
    """
    Trims conversation history to fit within the history token budget.
    Preserves the most recent turns; older turns are dropped first.
    Always keeps at least the last turn to maintain coherence.
    """

    def trim(
        self,
        turns: List[ConversationTurn],
        history_budget: int,
    ) -> Tuple[List[ConversationTurn], int]:
        """
        Returns (trimmed_turns, tokens_used).
        """
        if not turns:
            return [], 0

        kept = []
        tokens_used = 0
        for turn in reversed(turns):
            if tokens_used + turn.token_count > history_budget and kept:
                break
            kept.insert(0, turn)
            tokens_used += turn.token_count

        return kept, tokens_used
```

## Solution 4: Dynamic Context Assembler

```python
from typing import Any, Dict, List, Optional


@dataclass
class AssembledContext:
    system_prompt: str
    history: List[ConversationTurn]
    tool_results: List[str]
    retrieved_chunks: List[str]
    total_tokens_used: int
    budget: ContextBudget
    complexity: RequestComplexity
    truncations: Dict[str, int]   # component -> tokens dropped


class DynamicContextAssembler:
    """
    Assembles the full LLM context respecting per-component token budgets
    derived from the request complexity classification.
    """

    def __init__(
        self,
        policy: ContextBudgetPolicy,
        classifier: RequestComplexityClassifier,
        history_trimmer: BudgetedHistoryTrimmer,
        tokens_per_char: float = 0.25,
    ):
        self._policy = policy
        self._classifier = classifier
        self._trimmer = history_trimmer
        self._tpc = tokens_per_char

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) * self._tpc))

    def assemble(
        self,
        user_message: str,
        system_prompt: str,
        history: List[ConversationTurn],
        tool_results: List[str],
        retrieved_chunks: List[str],
        attached_document_count: int = 0,
    ) -> AssembledContext:
        complexity = self._classifier.classify(
            user_message,
            conversation_turns=len(history),
            attached_document_count=attached_document_count,
        )
        budget = self._policy.get(complexity)
        truncations: Dict[str, int] = {}

        # Trim history
        trimmed_history, history_tokens = self._trimmer.trim(
            history, budget.conversation_history_tokens
        )
        dropped_history = sum(t.token_count for t in history) - history_tokens
        if dropped_history > 0:
            truncations["history"] = dropped_history

        # Fit tool results
        tool_budget = budget.tool_result_tokens
        fitted_tools: List[str] = []
        tool_tokens_used = 0
        for result in tool_results:
            t = self._estimate_tokens(result)
            if tool_tokens_used + t <= tool_budget:
                fitted_tools.append(result)
                tool_tokens_used += t
            else:
                truncations["tool_results"] = truncations.get("tool_results", 0) + t

        # Fit retrieved chunks
        retrieval_budget = budget.retrieved_context_tokens
        fitted_chunks: List[str] = []
        retrieval_tokens_used = 0
        for chunk in retrieved_chunks:
            t = self._estimate_tokens(chunk)
            if retrieval_tokens_used + t <= retrieval_budget:
                fitted_chunks.append(chunk)
                retrieval_tokens_used += t
            else:
                truncations["retrieved_chunks"] = (
                    truncations.get("retrieved_chunks", 0) + t
                )

        total = (
            self._estimate_tokens(system_prompt)
            + history_tokens
            + tool_tokens_used
            + retrieval_tokens_used
        )

        return AssembledContext(
            system_prompt=system_prompt,
            history=trimmed_history,
            tool_results=fitted_tools,
            retrieved_chunks=fitted_chunks,
            total_tokens_used=total,
            budget=budget,
            complexity=complexity,
            truncations=truncations,
        )
```

## Solution 5: Context Sizing Stats Recorder

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class ContextSizingStatsRecorder:
    """
    Tracks context budget utilization across requests to surface
    whether complexity tiers are correctly calibrated.
    """

    def __init__(self):
        self._lock = Lock()
        self._records: List[dict] = []

    def record(self, assembled: AssembledContext) -> None:
        utilization = assembled.total_tokens_used / max(assembled.budget.total_tokens, 1)
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "complexity": assembled.complexity.value,
                "total_budget": assembled.budget.total_tokens,
                "tokens_used": assembled.total_tokens_used,
                "utilization": round(utilization, 4),
                "truncations": assembled.truncations,
                "had_truncation": bool(assembled.truncations),
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        by_complexity: Dict[str, List[float]] = defaultdict(list)
        truncation_counts: Dict[str, int] = defaultdict(int)

        for r in recent:
            by_complexity[r["complexity"]].append(r["utilization"])
            if r["had_truncation"]:
                truncation_counts[r["complexity"]] += 1

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "by_complexity": {
                c: {
                    "count": len(utils),
                    "avg_utilization": round(sum(utils) / len(utils), 4),
                    "truncation_rate": round(
                        truncation_counts[c] / len(utils), 4
                    ),
                }
                for c, utils in by_complexity.items()
            },
        }
```

## Solution 6: Context Sizing Dashboard

```python
import time


class ContextSizingDashboard:
    """
    Combines complexity classification outcomes and budget utilization
    into an operational view for tuning the budget table.
    """

    def __init__(
        self,
        classifier: RequestComplexityClassifier,
        policy: ContextBudgetPolicy,
        stats: ContextSizingStatsRecorder,
    ):
        self._classifier = classifier
        self._policy = policy
        self._stats = stats

    def render(self) -> dict:
        budgets = {
            c.value: {
                "total_tokens": self._policy.get(c).total_tokens,
                "history_budget": self._policy.get(c).conversation_history_tokens,
                "retrieval_budget": self._policy.get(c).retrieved_context_tokens,
                "tool_budget": self._policy.get(c).tool_result_tokens,
            }
            for c in RequestComplexity
        }
        return {
            "generated_at": time.time(),
            "budget_table": budgets,
            "utilization": self._stats.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Complexity Classification | Per-Component Budgets | History Trimming | Utilization Tracking | Dashboard |
|---|---|---|---|---|---|
| RequestComplexityClassifier | Yes (lexical signals) | No | No | No | No |
| ContextBudgetPolicy | No | Yes (5 tiers) | No | No | No |
| BudgetedHistoryTrimmer | No | No | Yes (recency) | No | No |
| DynamicContextAssembler | Via classifier | Via policy | Via trimmer | No | No |
| ContextSizingStatsRecorder | No | No | No | Yes (per-tier) | No |
| ContextSizingDashboard | No | No | No | No | Yes |

**Best for production**: Start with lexical classification (`RequestComplexityClassifier`) and tune the thresholds using `ContextSizingStatsRecorder` after one week of traffic — a `truncation_rate > 0.10` for a tier means the budget is too small; a `avg_utilization < 0.30` means the budget is too large. Set `TRIVIAL` total to 4096 tokens to cut cost on high-volume simple queries by up to 30×. Use the `complexity` label as a tag on LLM API call metrics to correlate cost per complexity tier and verify that the classification is accurate before sizing down.
