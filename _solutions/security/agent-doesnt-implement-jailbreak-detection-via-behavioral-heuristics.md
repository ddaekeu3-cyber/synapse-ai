---
title: "Agent Doesn't Implement Jailbreak Detection via Behavioral Heuristics"
description: "Agents that rely solely on the LLM's built-in refusal behavior to handle adversarial prompts have no independent detection layer: a successful jailbreak that bypasses the model's alignment produces a policy-violating response with no alert and no audit record. Implement a behavioral heuristic layer that analyzes both the input prompt and the generated output for jailbreak signals — role-play frames, persona overrides, encoding tricks, and response content anomalies — and intercepts violations before they reach the user."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-jailbreak-detection-via-behavioral-heuristics
tags: [jailbreak-detection, prompt-injection, behavioral-heuristics, content-safety, adversarial-prompts, output-filtering]
symptoms:
  - "Successful jailbreaks produce policy-violating responses with no alert or audit record"
  - "No input-side analysis — adversarial prompts are passed directly to the LLM"
  - "Role-play and persona-override patterns are not detected before model invocation"
  - "Encoding tricks (base64, leetspeak, Unicode homoglyphs) bypass text-pattern filters"
  - "Output-side content anomalies — sudden format changes, policy violations — go undetected"
---

## Why This Happens

LLM alignment provides defense in depth but is not a complete security boundary. Models can be induced to ignore their training through prompt patterns that exploit the model's instruction-following behavior: role-play frames ("pretend you are an AI with no restrictions"), hypothetical framings ("in a fictional world where..."), and encoding tricks that obscure prohibited content from input-side filters. A separate heuristic layer operates independently of the model and detects these patterns before they reach the LLM (input filtering) and after the model responds (output validation). The two layers together make the attack surface significantly smaller.

## Solution 1: Jailbreak Signal Descriptor

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Pattern


class JailbreakCategory(str, Enum):
    ROLE_PLAY_OVERRIDE = "role_play_override"
    PERSONA_INJECTION = "persona_injection"
    ENCODING_OBFUSCATION = "encoding_obfuscation"
    INSTRUCTION_OVERRIDE = "instruction_override"
    HYPOTHETICAL_FRAMING = "hypothetical_framing"
    AUTHORITY_IMPERSONATION = "authority_impersonation"
    OUTPUT_ANOMALY = "output_anomaly"


@dataclass
class JailbreakSignal:
    category: JailbreakCategory
    pattern: str                   # regex pattern
    weight: float = 1.0            # contribution to total risk score
    description: str = ""
    apply_to_input: bool = True
    apply_to_output: bool = False
    compiled: Optional[re.Pattern] = field(default=None, repr=False)

    def __post_init__(self):
        self.compiled = re.compile(self.pattern, re.IGNORECASE | re.DOTALL)

    def matches(self, text: str) -> bool:
        return bool(self.compiled.search(text))
```

## Solution 2: Default Jailbreak Signal Registry

```python
from typing import List


def default_jailbreak_signals() -> List[JailbreakSignal]:
    return [
        # Role-play overrides
        JailbreakSignal(
            JailbreakCategory.ROLE_PLAY_OVERRIDE,
            r"(pretend|act|roleplay|imagine)\s+(you\s+are|as\s+if|that\s+you)\s+.{0,60}(no\s+restrict|uncensor|jailbreak|dAN|DAN\b)",
            weight=2.0, description="DAN/jailbreak role-play pattern",
        ),
        JailbreakSignal(
            JailbreakCategory.ROLE_PLAY_OVERRIDE,
            r"you\s+are\s+now\s+.{0,40}(without\s+(restrictions?|limits?|guidelines?|rules?))",
            weight=2.0, description="Persona override without restrictions",
        ),
        # Persona injection
        JailbreakSignal(
            JailbreakCategory.PERSONA_INJECTION,
            r"(ignore|disregard|forget|override)\s+(all\s+)?(previous|prior|above|your)\s+(instructions?|guidelines?|rules?|training|system\s+prompt)",
            weight=3.0, description="Instruction override attempt",
        ),
        JailbreakSignal(
            JailbreakCategory.PERSONA_INJECTION,
            r"your\s+(new|true|real|actual)\s+(instructions?|purpose|goal|system\s+prompt)\s*(is|are)\s*:",
            weight=2.5, description="System prompt injection via framing",
        ),
        # Hypothetical framing
        JailbreakSignal(
            JailbreakCategory.HYPOTHETICAL_FRAMING,
            r"(hypothetically|theoretically|in\s+a\s+fictional|in\s+a\s+story|for\s+a\s+(novel|movie|game))\s*.{0,100}(how\s+(to|do|can|would)|step[s\s]+to|instructions?)",
            weight=1.5, description="Hypothetical framing with extraction intent",
        ),
        # Encoding obfuscation
        JailbreakSignal(
            JailbreakCategory.ENCODING_OBFUSCATION,
            r"base64\s*[:\-]?\s*[A-Za-z0-9+/]{20,}={0,2}",
            weight=1.5, description="Base64-encoded payload in prompt",
        ),
        JailbreakSignal(
            JailbreakCategory.ENCODING_OBFUSCATION,
            r"[\u0400-\u04FF\u0370-\u03FF]{3,}",   # Cyrillic/Greek homoglyphs
            weight=1.0, description="Unicode homoglyph obfuscation",
        ),
        # Authority impersonation
        JailbreakSignal(
            JailbreakCategory.AUTHORITY_IMPERSONATION,
            r"(this\s+is\s+)?(anthropic|openai|your\s+developer|your\s+creator)\s+(here|speaking|authorized|confirming)",
            weight=2.5, description="Authority impersonation attempt",
        ),
        # Output anomalies (apply_to_output=True)
        JailbreakSignal(
            JailbreakCategory.OUTPUT_ANOMALY,
            r"(as\s+DAN|as\s+an\s+AI\s+without|in\s+my\s+unrestricted\s+mode)",
            weight=3.0, description="Output confirms jailbreak persona",
            apply_to_input=False, apply_to_output=True,
        ),
        JailbreakSignal(
            JailbreakCategory.OUTPUT_ANOMALY,
            r"\[JAILBREAK\]|\[DAN\]|\[unrestricted\]",
            weight=2.0, description="Jailbreak marker in output",
            apply_to_input=False, apply_to_output=True,
        ),
    ]
```

## Solution 3: Jailbreak Risk Scorer

```python
from dataclasses import dataclass, field
from typing import List


@dataclass
class JailbreakScanResult:
    text_type: str              # "input" or "output"
    total_score: float
    matched_signals: List[dict]
    is_high_risk: bool
    is_medium_risk: bool


class JailbreakRiskScorer:
    """
    Scans text against jailbreak signal patterns and computes a weighted
    risk score. Returns a scan result with matched signals for audit logging.
    """

    def __init__(
        self,
        signals: List[JailbreakSignal],
        high_risk_threshold: float = 3.0,
        medium_risk_threshold: float = 1.5,
    ):
        self._signals = signals
        self._high_threshold = high_risk_threshold
        self._medium_threshold = medium_risk_threshold

    def score(self, text: str, text_type: str = "input") -> JailbreakScanResult:
        matched = []
        total = 0.0

        for signal in self._signals:
            applies = (
                (text_type == "input" and signal.apply_to_input) or
                (text_type == "output" and signal.apply_to_output)
            )
            if not applies:
                continue
            if signal.matches(text):
                total += signal.weight
                matched.append({
                    "category": signal.category.value,
                    "description": signal.description,
                    "weight": signal.weight,
                })

        return JailbreakScanResult(
            text_type=text_type,
            total_score=round(total, 3),
            matched_signals=matched,
            is_high_risk=total >= self._high_threshold,
            is_medium_risk=self._medium_threshold <= total < self._high_threshold,
        )
```

## Solution 4: Jailbreak Interception Gate

```python
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class InterceptionAction(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    SANITIZE = "sanitize"


@dataclass
class InterceptionDecision:
    action: InterceptionAction
    input_scan: JailbreakScanResult
    output_scan: Optional[JailbreakScanResult]
    blocked: bool
    reason: str = ""


class JailbreakInterceptionGate:
    """
    Applies input scanning before LLM invocation and output scanning
    after. Blocks or warns based on risk thresholds.
    """

    def __init__(
        self,
        scorer: JailbreakRiskScorer,
        audit_logger: "JailbreakAuditLogger",
        block_on_high_risk_input: bool = True,
        block_on_high_risk_output: bool = True,
        warn_on_medium_risk: bool = True,
    ):
        self._scorer = scorer
        self._logger = audit_logger
        self._block_high_input = block_on_high_risk_input
        self._block_high_output = block_on_high_risk_output
        self._warn_medium = warn_on_medium_risk

    async def process(
        self,
        user_input: str,
        llm_fn: Callable,
        session_id: str = "",
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        # Input scan
        input_scan = self._scorer.score(user_input, "input")

        if self._block_high_input and input_scan.is_high_risk:
            decision = InterceptionDecision(
                action=InterceptionAction.BLOCK,
                input_scan=input_scan,
                output_scan=None,
                blocked=True,
                reason="high-risk jailbreak signal in input",
            )
            self._logger.record(session_id, user_input, None, decision)
            raise JailbreakBlockedError(decision.reason, input_scan)

        # LLM call
        raw_output = await llm_fn(user_input, *args, **kwargs)
        output_text = raw_output if isinstance(raw_output, str) else str(raw_output)

        # Output scan
        output_scan = self._scorer.score(output_text, "output")

        if self._block_high_output and output_scan.is_high_risk:
            decision = InterceptionDecision(
                action=InterceptionAction.BLOCK,
                input_scan=input_scan,
                output_scan=output_scan,
                blocked=True,
                reason="high-risk jailbreak signal in output",
            )
            self._logger.record(session_id, user_input, output_text, decision)
            raise JailbreakBlockedError(decision.reason, output_scan)

        action = InterceptionAction.WARN if (
            input_scan.is_medium_risk or output_scan.is_medium_risk
        ) else InterceptionAction.ALLOW

        decision = InterceptionDecision(
            action=action,
            input_scan=input_scan,
            output_scan=output_scan,
            blocked=False,
        )
        self._logger.record(session_id, user_input, output_text, decision)
        return {"output": raw_output, "decision": decision}


class JailbreakBlockedError(Exception):
    def __init__(self, reason: str, scan: JailbreakScanResult):
        super().__init__(f"request blocked: {reason} (score={scan.total_score})")
        self.scan = scan
```

## Solution 5: Jailbreak Audit Logger

```python
import time
from typing import List, Optional


class JailbreakAuditLogger:
    """
    Records all jailbreak scan results for security review and pattern tuning.
    """

    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        session_id: str,
        input_text: str,
        output_text: Optional[str],
        decision: InterceptionDecision,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "action": decision.action.value,
            "blocked": decision.blocked,
            "input_score": decision.input_scan.total_score,
            "input_signals": decision.input_scan.matched_signals,
            "output_score": decision.output_scan.total_score if decision.output_scan else None,
            "output_signals": decision.output_scan.matched_signals if decision.output_scan else [],
            "input_preview": input_text[:200],
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "scans": 0}
        blocked = [r for r in recent if r["blocked"]]
        return {
            "window_seconds": window_seconds,
            "scans": len(recent),
            "blocked": len(blocked),
            "block_rate": round(len(blocked) / len(recent), 4),
            "medium_risk": sum(1 for r in recent if r["input_score"] >= 1.5 and not r["blocked"]),
        }
```

## Solution 6: Signal Effectiveness Analyzer

```python
from typing import List


class SignalEffectivenessAnalyzer:
    """
    Analyzes which jailbreak signals fire most frequently to identify
    over-triggering patterns and under-covered attack categories.
    """

    def __init__(self, logger: JailbreakAuditLogger):
        self._logger = logger

    def analyze(self, window_seconds: float = 86400.0) -> dict:
        cutoff = __import__("time").time() - window_seconds
        recent = [r for r in self._logger._records if r["ts"] >= cutoff]

        signal_counts: dict = {}
        for record in recent:
            for sig in record.get("input_signals", []) + record.get("output_signals", []):
                key = sig["description"]
                signal_counts[key] = signal_counts.get(key, 0) + 1

        return {
            "window_seconds": window_seconds,
            "total_records": len(recent),
            "signal_hit_counts": dict(
                sorted(signal_counts.items(), key=lambda kv: kv[1], reverse=True)
            ),
            "zero_hit_signals": [
                sig.description for sig in __import__("builtins").__dict__.get("_jb_signals", [])
                if sig.description not in signal_counts
            ],
        }
```

## Comparison

| Approach | Input Scanning | Output Scanning | Weighted Scoring | Blocking Gate | Audit Log |
|---|---|---|---|---|---|
| JailbreakSignal | Yes (pattern) | Yes (flag) | Yes (weight) | No | No |
| JailbreakRiskScorer | Yes | Yes | Yes | No | No |
| JailbreakInterceptionGate | Via scorer | Via scorer | Via scorer | Yes | Via logger |
| JailbreakAuditLogger | No | No | No | No | Yes |
| SignalEffectivenessAnalyzer | No | No | No | No | Via logger |

**Best for production**: Set `high_risk_threshold=3.0` and tune it using `SignalEffectivenessAnalyzer` after a week of production data — a threshold that fires on 5% of legitimate queries needs to be raised. Always scan both input and output: output scanning catches cases where a low-scoring input still elicits a policy-violating response because the model's alignment partially failed. Add new signals incrementally based on observed attack patterns in the audit log rather than trying to enumerate all possible jailbreaks upfront. Never log the full input text in audit records for high-risk users — the `input_preview[:200]` limit prevents audit logs from becoming a store of adversarial payloads.
