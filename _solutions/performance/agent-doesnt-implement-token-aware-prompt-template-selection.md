---
title: "Agent Doesn't Implement Token-Aware Prompt Template Selection"
description: "Agents that use a single fixed prompt template for all requests pay the same token cost regardless of task complexity. A verbose chain-of-thought template applied to a simple lookup wastes tokens; a concise template applied to a complex reasoning task produces shallow answers. Implement token-aware prompt template selection that matches template verbosity to request complexity and available context budget, reducing cost on simple requests without sacrificing depth on complex ones."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-token-aware-prompt-template-selection
tags: [prompt-template, token-efficiency, cost-optimization, template-selection, context-budget, prompt-engineering]
symptoms:
  - "Same verbose system prompt used for every request regardless of complexity"
  - "Simple yes/no questions pay for multi-paragraph chain-of-thought instructions"
  - "Token budget exhausted by system prompt before user content is even included"
  - "No mechanism to select shorter prompt variants when context budget is tight"
  - "Cost per request is dominated by system prompt tokens, not actual content"
---

## Why This Happens

Prompt templates are typically written once for the hardest case — complex multi-step reasoning — and then used unchanged for all requests. This is safe but wasteful: a template with 2000 tokens of chain-of-thought instructions, tool descriptions, and examples is applied to a user who asked a factual question answerable in one sentence. Token-aware template selection requires classifying the request, estimating the available context budget after accounting for conversation history and expected tool results, and choosing the template variant whose token cost best fits the budget without over-specifying for simple requests.

## Solution 1: Prompt Template Variant

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class TemplateVerbosity(str, Enum):
    MINIMAL = "minimal"         # bare-minimum instructions, no examples
    CONCISE = "concise"         # short instructions, no chain-of-thought
    STANDARD = "standard"       # normal template with tool descriptions
    DETAILED = "detailed"       # chain-of-thought, examples, full tool docs
    EXHAUSTIVE = "exhaustive"   # maximum context: few-shot, all tool specs


@dataclass
class PromptTemplateVariant:
    verbosity: TemplateVerbosity
    template_text: str           # may contain {placeholders}
    estimated_tokens: int        # pre-computed token estimate
    min_complexity_score: int    # minimum request complexity score to use this
    supports_tools: bool = True
    supports_cot: bool = False   # chain-of-thought instructions included

    def render(self, variables: Dict[str, Any]) -> str:
        try:
            return self.template_text.format(**variables)
        except KeyError:
            return self.template_text
```

## Solution 2: Template Library

```python
from typing import Dict, List, Optional


class PromptTemplateLibrary:
    """
    Stores multiple variants for each named template family.
    Variants are indexed by verbosity level.
    """

    def __init__(self):
        self._families: Dict[str, Dict[TemplateVerbosity, PromptTemplateVariant]] = {}

    def register(self, family_name: str, variant: PromptTemplateVariant) -> None:
        if family_name not in self._families:
            self._families[family_name] = {}
        self._families[family_name][variant.verbosity] = variant

    def get(
        self,
        family_name: str,
        verbosity: TemplateVerbosity,
    ) -> Optional[PromptTemplateVariant]:
        family = self._families.get(family_name, {})
        return family.get(verbosity)

    def best_fit(
        self,
        family_name: str,
        max_tokens: int,
        min_verbosity: TemplateVerbosity = TemplateVerbosity.MINIMAL,
    ) -> Optional[PromptTemplateVariant]:
        """
        Returns the most verbose variant that fits within max_tokens.
        """
        family = self._families.get(family_name, {})
        verbosity_order = list(TemplateVerbosity)
        candidates = [
            v for v in family.values()
            if v.estimated_tokens <= max_tokens
            and verbosity_order.index(v.verbosity) >= verbosity_order.index(min_verbosity)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda v: verbosity_order.index(v.verbosity))

    def families(self) -> List[str]:
        return list(self._families.keys())
```

## Solution 3: Context Budget Calculator

```python
from dataclasses import dataclass
from typing import List


@dataclass
class ContextBudgetBreakdown:
    total_window: int
    reserved_for_output: int
    conversation_history_tokens: int
    tool_result_budget: int
    available_for_system_prompt: int


class ContextBudgetCalculator:
    """
    Computes how many tokens are available for the system prompt
    given the current conversation state and expected outputs.
    """

    def __init__(
        self,
        model_context_window: int = 128000,
        output_reserve: int = 4096,
        tokens_per_char: float = 0.25,
    ):
        self._window = model_context_window
        self._output_reserve = output_reserve
        self._tpc = tokens_per_char

    def _estimate(self, text: str) -> int:
        return max(1, int(len(text) * self._tpc))

    def calculate(
        self,
        conversation_history: List[str],
        expected_tool_result_chars: int = 2000,
    ) -> ContextBudgetBreakdown:
        history_tokens = sum(self._estimate(t) for t in conversation_history)
        tool_budget = self._estimate(" " * expected_tool_result_chars)
        used = history_tokens + tool_budget + self._output_reserve
        available = max(0, self._window - used)

        return ContextBudgetBreakdown(
            total_window=self._window,
            reserved_for_output=self._output_reserve,
            conversation_history_tokens=history_tokens,
            tool_result_budget=tool_budget,
            available_for_system_prompt=available,
        )
```

## Solution 4: Token-Aware Template Selector

```python
from typing import Any, Dict, List, Optional


@dataclass
class TemplateSelectionResult:
    variant: PromptTemplateVariant
    rendered_text: str
    tokens_used: int
    tokens_available: int
    verbosity_selected: TemplateVerbosity
    budget_utilization: float


class TokenAwareTemplateSelector:
    """
    Selects the most appropriate prompt template variant given
    the available token budget and request complexity.
    """

    def __init__(
        self,
        library: PromptTemplateLibrary,
        budget_calculator: ContextBudgetCalculator,
    ):
        self._library = library
        self._budget_calculator = budget_calculator

    def select(
        self,
        family_name: str,
        conversation_history: List[str],
        template_variables: Dict[str, Any],
        complexity_score: int = 0,
        expected_tool_result_chars: int = 2000,
    ) -> Optional[TemplateSelectionResult]:
        budget = self._budget_calculator.calculate(
            conversation_history, expected_tool_result_chars
        )
        available = budget.available_for_system_prompt

        # Determine minimum verbosity based on complexity
        if complexity_score <= 2:
            min_verbosity = TemplateVerbosity.MINIMAL
        elif complexity_score <= 5:
            min_verbosity = TemplateVerbosity.CONCISE
        elif complexity_score <= 8:
            min_verbosity = TemplateVerbosity.STANDARD
        else:
            min_verbosity = TemplateVerbosity.DETAILED

        variant = self._library.best_fit(
            family_name, max_tokens=available, min_verbosity=min_verbosity
        )
        if variant is None:
            # Fall back to minimal regardless of budget
            variant = self._library.get(family_name, TemplateVerbosity.MINIMAL)
        if variant is None:
            return None

        rendered = variant.render(template_variables)
        return TemplateSelectionResult(
            variant=variant,
            rendered_text=rendered,
            tokens_used=variant.estimated_tokens,
            tokens_available=available,
            verbosity_selected=variant.verbosity,
            budget_utilization=round(variant.estimated_tokens / max(available, 1), 4),
        )
```

## Solution 5: Template Token Savings Recorder

```python
import time
from threading import Lock
from typing import List


class TemplateTokenSavingsRecorder:
    """
    Compares selected template token cost against the maximum template
    cost to compute per-request savings from dynamic selection.
    """

    def __init__(self, max_template_tokens: int):
        self._max_tokens = max_template_tokens
        self._lock = Lock()
        self._records: List[dict] = []

    def record(self, result: TemplateSelectionResult) -> None:
        saved = self._max_tokens - result.tokens_used
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "verbosity": result.verbosity_selected.value,
                "tokens_used": result.tokens_used,
                "tokens_saved": max(saved, 0),
                "budget_utilization": result.budget_utilization,
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "selections": 0}

        from collections import Counter
        total_saved = sum(r["tokens_saved"] for r in recent)
        verbosity_counts = Counter(r["verbosity"] for r in recent)

        return {
            "window_seconds": window_seconds,
            "selections": len(recent),
            "total_tokens_saved": total_saved,
            "avg_tokens_saved_per_request": round(total_saved / len(recent), 1),
            "verbosity_distribution": dict(verbosity_counts),
        }
```

## Solution 6: Template Selection Dashboard

```python
import time


class TemplateSelectionDashboard:
    """
    Combines library state, budget calculator parameters,
    and savings data into a single operational view.
    """

    def __init__(
        self,
        library: PromptTemplateLibrary,
        calculator: ContextBudgetCalculator,
        savings_recorder: TemplateTokenSavingsRecorder,
    ):
        self._library = library
        self._calculator = calculator
        self._savings = savings_recorder

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "registered_families": self._library.families(),
            "model_context_window": self._calculator._window,
            "output_reserve": self._calculator._output_reserve,
            "savings_last_hour": self._savings.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Variant Storage | Budget Calculation | Complexity-Aware | Savings Tracking | Dashboard |
|---|---|---|---|---|---|
| PromptTemplateLibrary | Yes (by verbosity) | No | No | No | No |
| ContextBudgetCalculator | No | Yes | No | No | No |
| TokenAwareTemplateSelector | Via library | Via calculator | Yes | No | No |
| TemplateTokenSavingsRecorder | No | No | No | Yes | No |
| TemplateSelectionDashboard | No | No | No | No | Yes |

**Best for production**: Pre-compute `estimated_tokens` for each variant at registration time using the same tokenizer the model uses — character estimates drift for non-English content and code-heavy prompts. Keep `MINIMAL` variants under 200 tokens for the highest-volume simple queries; the cost savings on millions of calls per day are significant. Monitor `verbosity_distribution` in `TemplateTokenSavingsRecorder`: if 80%+ of requests use `DETAILED` or `EXHAUSTIVE`, the complexity classifier needs recalibration. Set `output_reserve` to your P95 output length in tokens to prevent the system prompt from crowding out generation on long-answer requests.
