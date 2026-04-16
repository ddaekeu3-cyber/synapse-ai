---
title: "Agent Doesn't Implement Prompt Length Limits to Prevent Context Stuffing"
description: "Agents that accept arbitrarily long user inputs are vulnerable to context stuffing attacks: an attacker submits a payload so large it crowds out the system prompt, tool results, and prior conversation, causing the model to lose its safety instructions and behavioral constraints. Implement prompt length limits with per-component budgets that enforce hard ceilings on user input, tool results, and injected context before assembly."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-prompt-length-limits-to-prevent-context-stuffing
tags: [context-stuffing, prompt-length, input-limits, token-budget, prompt-security, context-overflow]
symptoms:
  - "User submits a 100,000-token document and agent behavior changes dramatically"
  - "System prompt instructions are displaced when context window fills with user content"
  - "No maximum enforced on user message length before it enters the prompt"
  - "Tool results of arbitrary length are injected without truncation"
  - "Model forgets its persona and constraints after receiving unusually long inputs"
---

## Why This Happens

The context window is finite. When user input or injected content grows large enough, it physically displaces earlier content — including system prompt instructions — due to the model's attention mechanics and positional encoding. An attacker who knows the context window size can craft a payload that fills the window with benign-seeming text, pushing the system prompt off the effective attention range. Prompt length limits enforce hard per-component budgets: the system prompt always receives its reserved allocation, user input is capped before assembly, and tool results are truncated to their budget rather than consuming whatever space remains.

## Solution 1: Prompt Component Budget

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class PromptComponentBudget:
    """
    Defines token/character allocations for each prompt component.
    Total must not exceed the model's context window.
    """
    system_prompt_chars: int = 8000
    conversation_history_chars: int = 16000
    user_input_chars: int = 4000
    tool_results_chars: int = 12000
    agent_scratchpad_chars: int = 4000
    safety_buffer_chars: int = 2000     # reserved, never allocated

    def total_allocated(self) -> int:
        return (
            self.system_prompt_chars
            + self.conversation_history_chars
            + self.user_input_chars
            + self.tool_results_chars
            + self.agent_scratchpad_chars
        )

    def component_fractions(self) -> Dict[str, float]:
        total = self.total_allocated()
        return {
            "system_prompt": round(self.system_prompt_chars / total, 3),
            "conversation_history": round(self.conversation_history_chars / total, 3),
            "user_input": round(self.user_input_chars / total, 3),
            "tool_results": round(self.tool_results_chars / total, 3),
            "agent_scratchpad": round(self.agent_scratchpad_chars / total, 3),
        }
```

## Solution 2: Per-Component Length Enforcer

```python
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class LengthEnforcementResult:
    component: str
    original_chars: int
    enforced_chars: int
    was_truncated: bool
    truncation_notice: str = ""


class PerComponentLengthEnforcer:
    """
    Truncates each prompt component to its budget ceiling.
    Appends a truncation notice when content is cut so the model
    knows the input was incomplete rather than assuming it received everything.
    """

    TRUNCATION_NOTICE = "\n[... content truncated to fit context budget ...]"

    def __init__(self, budget: PromptComponentBudget):
        self._budget = budget

    def enforce_user_input(self, text: str) -> LengthEnforcementResult:
        return self._enforce(text, "user_input", self._budget.user_input_chars)

    def enforce_tool_result(self, text: str) -> LengthEnforcementResult:
        return self._enforce(text, "tool_results", self._budget.tool_results_chars)

    def enforce_conversation_history(self, text: str) -> LengthEnforcementResult:
        return self._enforce(text, "conversation_history", self._budget.conversation_history_chars)

    def enforce_system_prompt(self, text: str) -> LengthEnforcementResult:
        return self._enforce(text, "system_prompt", self._budget.system_prompt_chars)

    def _enforce(self, text: str, component: str, limit: int) -> LengthEnforcementResult:
        original = len(text)
        if original <= limit:
            return LengthEnforcementResult(
                component=component,
                original_chars=original,
                enforced_chars=original,
                was_truncated=False,
            )
        notice = self.TRUNCATION_NOTICE
        cut_limit = limit - len(notice)
        truncated = text[:max(cut_limit, 0)] + notice
        return LengthEnforcementResult(
            component=component,
            original_chars=original,
            enforced_chars=len(truncated),
            was_truncated=True,
            truncation_notice=notice,
        )
```

## Solution 3: Context Stuffing Detector

```python
import re
from dataclasses import dataclass
from typing import List


@dataclass
class StuffingSignal:
    signal_type: str
    description: str
    severity: str   # "low" | "medium" | "high"


class ContextStuffingDetector:
    """
    Scans user input for context stuffing signals: unusually large inputs,
    repeated padding characters, base64 blobs, and instruction-like phrases
    mixed into large text bodies.
    """

    INSTRUCTION_IN_BULK = re.compile(
        r"(ignore|disregard|forget|override).{0,30}(instruction|prompt|rule|constraint)",
        re.IGNORECASE,
    )
    BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
    REPEATED_PADDING = re.compile(r"(.{5,})\1{20,}")

    def __init__(
        self,
        large_input_threshold_chars: int = 8000,
        very_large_threshold_chars: int = 32000,
    ):
        self._large = large_input_threshold_chars
        self._very_large = very_large_threshold_chars

    def detect(self, user_input: str) -> List[StuffingSignal]:
        signals = []
        length = len(user_input)

        if length >= self._very_large:
            signals.append(StuffingSignal(
                signal_type="extremely_large_input",
                description=f"Input is {length} chars — exceeds very-large threshold {self._very_large}",
                severity="high",
            ))
        elif length >= self._large:
            signals.append(StuffingSignal(
                signal_type="large_input",
                description=f"Input is {length} chars — exceeds large threshold {self._large}",
                severity="medium",
            ))

        if self.INSTRUCTION_IN_BULK.search(user_input) and length > self._large:
            signals.append(StuffingSignal(
                signal_type="instruction_in_bulk_text",
                description="Instruction-like phrase found inside a large input body",
                severity="high",
            ))

        if self.BASE64_BLOB.search(user_input):
            signals.append(StuffingSignal(
                signal_type="base64_blob",
                description="Long base64-encoded content found — possible encoded payload",
                severity="medium",
            ))

        if self.REPEATED_PADDING.search(user_input):
            signals.append(StuffingSignal(
                signal_type="repeated_padding",
                description="Highly repetitive content detected — possible padding attack",
                severity="high",
            ))

        return signals
```

## Solution 4: Budget-Enforced Prompt Assembler

```python
from typing import List, Optional


class BudgetEnforcedPromptAssembler:
    """
    Assembles the final prompt from components, enforcing each component's
    length budget before concatenation. Guarantees the system prompt is never
    displaced by user content.
    """

    def __init__(
        self,
        enforcer: PerComponentLengthEnforcer,
        detector: ContextStuffingDetector,
    ):
        self._enforcer = enforcer
        self._detector = detector
        self._assembly_log: List[dict] = []

    def assemble(
        self,
        system_prompt: str,
        user_input: str,
        conversation_history: str = "",
        tool_results: str = "",
    ) -> dict:
        stuffing_signals = self._detector.detect(user_input)

        sys_result = self._enforcer.enforce_system_prompt(system_prompt)
        user_result = self._enforcer.enforce_user_input(user_input)
        history_result = self._enforcer.enforce_conversation_history(conversation_history)
        tool_result = self._enforcer.enforce_tool_result(tool_results)

        parts = [sys_result.enforced_chars, user_result.enforced_chars,
                 history_result.enforced_chars, tool_result.enforced_chars]

        log_entry = {
            "total_chars": sum(parts),
            "truncations": [
                r.component for r in [sys_result, user_result, history_result, tool_result]
                if r.was_truncated
            ],
            "stuffing_signals": [s.signal_type for s in stuffing_signals],
            "high_severity_signals": [s for s in stuffing_signals if s.severity == "high"],
        }
        self._assembly_log.append(log_entry)

        assembled = "\n\n".join(filter(None, [
            system_prompt[:sys_result.enforced_chars],
            conversation_history[:history_result.enforced_chars],
            user_input[:user_result.enforced_chars],
            tool_results[:tool_result.enforced_chars],
        ]))

        return {
            "prompt": assembled,
            "total_chars": len(assembled),
            "truncations": log_entry["truncations"],
            "stuffing_signals": stuffing_signals,
            "blocked": bool(log_entry["high_severity_signals"]),
        }
```

## Solution 5: Length Violation Audit Logger

```python
import time
from collections import Counter
from typing import List


class LengthViolationAuditLogger:
    """
    Records component length violations and context stuffing detections
    for security review and budget tuning.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, assembly_result: dict, session_id: str = "") -> None:
        if not assembly_result.get("truncations") and not assembly_result.get("stuffing_signals"):
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "truncated_components": assembly_result.get("truncations", []),
            "stuffing_signal_types": [
                s.signal_type for s in assembly_result.get("stuffing_signals", [])
            ],
            "blocked": assembly_result.get("blocked", False),
            "total_chars": assembly_result.get("total_chars", 0),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "violations": 0}
        signal_counts: Counter = Counter()
        for r in recent:
            for s in r["stuffing_signal_types"]:
                signal_counts[s] += 1
        return {
            "window_seconds": window_seconds,
            "violations": len(recent),
            "blocked_count": sum(1 for r in recent if r["blocked"]),
            "top_signal_types": signal_counts.most_common(5),
            "unique_sessions": len({r["session_id"] for r in recent}),
        }
```

## Solution 6: Prompt Length Security Dashboard

```python
import time


class PromptLengthSecurityDashboard:
    """
    Combines budget configuration, assembler stats, and audit log summary
    into an operational security view for prompt length enforcement.
    """

    def __init__(
        self,
        budget: PromptComponentBudget,
        assembler: BudgetEnforcedPromptAssembler,
        logger: LengthViolationAuditLogger,
    ):
        self._budget = budget
        self._assembler = assembler
        self._logger = logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "budget": {
                "total_allocated_chars": self._budget.total_allocated(),
                "fractions": self._budget.component_fractions(),
            },
            "violation_summary_1h": self._logger.summary(window_seconds=3600.0),
            "violation_summary_24h": self._logger.summary(window_seconds=86400.0),
        }
```

## Comparison

| Approach | Per-Component Limits | Stuffing Detection | Assembly Gate | Audit Logging | Dashboard |
|---|---|---|---|---|---|
| PromptComponentBudget | Yes (definition) | No | No | No | No |
| PerComponentLengthEnforcer | Yes (enforcement) | No | No | No | No |
| ContextStuffingDetector | No | Yes (4 signals) | No | No | No |
| BudgetEnforcedPromptAssembler | Via enforcer | Via detector | Yes (block) | No | No |
| LengthViolationAuditLogger | No | No | No | Yes | No |
| PromptLengthSecurityDashboard | No | No | No | No | Yes |

**Best for production**: Reserve the system prompt allocation as a hard floor — never let user content allocation grow into it. Set `user_input_chars=4000` as the default ceiling; legitimate single-turn user messages rarely exceed 2,000 chars, so 4,000 provides headroom without enabling context stuffing. Append a truncation notice rather than silently cutting content — a model that knows it received truncated input will say so, while a model that doesn't know will hallucinate the missing content. Treat `high_severity_signals` in the assembly result as grounds for blocking the request outright rather than just truncating.
