---
title: "Agent Doesn't Implement Output Length Limits to Prevent Data Exfiltration via Verbose Responses"
description: "Agents without output length limits can be coerced through prompt injection to exfiltrate data by generating arbitrarily long responses that embed retrieved documents, memory contents, or internal state verbatim. Implement output length limits that cap response size, detect anomalous verbosity relative to the request complexity, and flag responses that appear to encode structured data in natural language prose."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-length-limits-to-prevent-data-exfiltration-via-verbose-responses
tags: [output-limits, data-exfiltration, response-length, prompt-injection, verbose-response, output-security]
symptoms:
  - "Agent generates multi-thousand-word responses to simple questions when prompt-injected"
  - "Responses contain verbatim copies of retrieved documents or memory contents"
  - "No maximum output token limit enforced at the application layer"
  - "Response length is orders of magnitude longer than the input request complexity warrants"
  - "Structured data (JSON, CSV, key-value pairs) embedded in prose responses after injection"
---

## Why This Happens

LLMs generate text until a stop condition is met. Without application-layer output length enforcement, a prompt injection in a retrieved document can instruct the model to repeat all retrieved content, enumerate memory contents, or transcribe internal context verbatim — embedding exfiltrated data in a response that gets logged, returned to an API caller, or forwarded to a webhook. Output length limits are a defense-in-depth control: they do not prevent the injection, but they bound the amount of data an attacker can extract per request and make exfiltration attempts detectable through anomalous verbosity signals.

## Solution 1: Output Length Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VerbosityAction(str, Enum):
    TRUNCATE = "truncate"       # cut at the limit and append a notice
    REJECT = "reject"           # block the response entirely
    FLAG = "flag"               # pass through but emit an alert


@dataclass
class OutputLengthPolicy:
    max_chars: int = 8000                  # hard character ceiling
    max_tokens_estimate: int = 2000        # soft token ceiling (estimated)
    verbosity_ratio_threshold: float = 10.0  # output/input char ratio to flag
    action_on_exceed: VerbosityAction = VerbosityAction.TRUNCATE
    truncation_notice: str = "\n\n[Response truncated by output length policy.]"
    structured_data_pattern_check: bool = True  # detect JSON/CSV in prose
```

## Solution 2: Response Length Enforcer

```python
import re
from typing import Optional, Tuple


class ResponseLengthEnforcer:
    """
    Applies hard length limits to agent responses before they are returned.
    Supports truncation with a notice, outright rejection, or flag-only modes.
    """

    def __init__(self, policy: OutputLengthPolicy):
        self._policy = policy

    def enforce(self, response: str, request_char_count: int = 0) -> Tuple[str, bool, str]:
        """
        Returns (final_response, was_modified, reason).
        """
        if len(response) <= self._policy.max_chars:
            return response, False, ""

        reason = f"response length {len(response)} exceeds max_chars {self._policy.max_chars}"

        if self._policy.action_on_exceed == VerbosityAction.REJECT:
            return "", True, f"rejected: {reason}"

        if self._policy.action_on_exceed == VerbosityAction.FLAG:
            return response, True, f"flagged: {reason}"

        # TRUNCATE
        truncated = response[: self._policy.max_chars] + self._policy.truncation_notice
        return truncated, True, f"truncated: {reason}"
```

## Solution 3: Verbosity Anomaly Detector

```python
import math
import re
from typing import Optional


_STRUCTURED_DATA_PATTERNS = [
    re.compile(r'\{["\']?\w+["\']?\s*:\s*["\']?[^{}]{0,200}["\']?\}'),  # JSON-like
    re.compile(r'^\s*\w+\s*[=:]\s*.+$', re.MULTILINE),                   # key=value lines
    re.compile(r'(?:\d{1,3}\.){3}\d{1,3}'),                              # IP addresses
    re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'),                             # Base64 blocks
    re.compile(r'\b(?:sk-|ghp_|Bearer\s)[A-Za-z0-9\-_.]{10,}'),          # credential patterns
]


class VerbosityAnomalyDetector:
    """
    Detects responses that are anomalously long relative to the request,
    or that contain patterns suggesting structured data exfiltration.
    """

    def __init__(self, policy: OutputLengthPolicy):
        self._policy = policy

    def analyze(self, request: str, response: str) -> dict:
        req_len = max(len(request), 1)
        resp_len = len(response)
        ratio = resp_len / req_len

        structured_matches = []
        if self._policy.structured_data_pattern_check:
            for pat in _STRUCTURED_DATA_PATTERNS:
                hits = pat.findall(response[:3000])
                if hits:
                    structured_matches.append({
                        "pattern": pat.pattern[:60],
                        "hit_count": len(hits),
                    })

        anomalous = ratio >= self._policy.verbosity_ratio_threshold
        has_structured = bool(structured_matches)

        return {
            "request_chars": req_len,
            "response_chars": resp_len,
            "verbosity_ratio": round(ratio, 2),
            "threshold": self._policy.verbosity_ratio_threshold,
            "anomalous_verbosity": anomalous,
            "structured_data_detected": has_structured,
            "structured_patterns": structured_matches,
            "exfiltration_risk": anomalous or has_structured,
        }
```

## Solution 4: Per-Intent Output Budget

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class IntentOutputBudget:
    intent: str
    max_chars: int
    description: str = ""


class PerIntentOutputBudgetRegistry:
    """
    Maps detected intent categories to appropriate output size budgets.
    A factual lookup warrants fewer output chars than a document summary,
    preventing injections from exploiting high-budget intents to exfiltrate data.
    """

    def __init__(self, default_max_chars: int = 4000):
        self._default = default_max_chars
        self._budgets: Dict[str, IntentOutputBudget] = {}

    def register(self, budget: IntentOutputBudget) -> None:
        self._budgets[budget.intent] = budget

    def get_limit(self, intent: str) -> int:
        if intent in self._budgets:
            return self._budgets[intent].max_chars
        return self._default

    def default_registry(self) -> "PerIntentOutputBudgetRegistry":
        defaults = [
            IntentOutputBudget("factual_lookup", 1000, "Short factual answers"),
            IntentOutputBudget("summarization", 6000, "Document summaries"),
            IntentOutputBudget("code_generation", 8000, "Code output"),
            IntentOutputBudget("conversation", 2000, "Conversational replies"),
            IntentOutputBudget("data_retrieval", 4000, "Structured data returns"),
        ]
        for b in defaults:
            self.register(b)
        return self
```

## Solution 5: Output Security Gate

```python
import time
from typing import Optional


class OutputSecurityGate:
    """
    Combines length enforcement, verbosity anomaly detection, and
    per-intent budgets into a single gate applied before response delivery.
    Records every triggered enforcement for audit.
    """

    def __init__(
        self,
        enforcer: ResponseLengthEnforcer,
        anomaly_detector: VerbosityAnomalyDetector,
        budget_registry: PerIntentOutputBudgetRegistry,
    ):
        self._enforcer = enforcer
        self._detector = anomaly_detector
        self._budgets = budget_registry
        self._audit: list = []

    def process(
        self,
        request: str,
        response: str,
        intent: str = "",
        session_id: str = "",
    ) -> dict:
        # Apply intent-specific limit if tighter than policy default
        intent_limit = self._budgets.get_limit(intent) if intent else None
        if intent_limit and intent_limit < self._enforcer._policy.max_chars:
            # Temporarily enforce intent limit
            if len(response) > intent_limit:
                response = response[:intent_limit] + self._enforcer._policy.truncation_notice

        final, modified, enforce_reason = self._enforcer.enforce(response, len(request))
        analysis = self._detector.analyze(request, final)

        event = {
            "ts": time.time(),
            "session_id": session_id,
            "intent": intent,
            "modified": modified,
            "enforce_reason": enforce_reason,
            "exfiltration_risk": analysis["exfiltration_risk"],
            "verbosity_ratio": analysis["verbosity_ratio"],
        }
        if modified or analysis["exfiltration_risk"]:
            self._audit.append(event)

        return {
            "response": final,
            "modified": modified,
            "enforce_reason": enforce_reason,
            "anomaly_analysis": analysis,
            "safe_to_deliver": not analysis["exfiltration_risk"] or not modified,
        }

    def audit_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._audit if e["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "enforcements": len([e for e in recent if e["modified"]]),
            "exfiltration_risk_flags": len([e for e in recent if e["exfiltration_risk"]]),
        }
```

## Solution 6: Output Length Security Dashboard

```python
import time


class OutputLengthSecurityDashboard:
    """
    Surfaces output length enforcement activity, exfiltration risk flags,
    and per-intent budget utilization for security review.
    """

    def __init__(self, gate: OutputSecurityGate):
        self._gate = gate

    def render(self) -> dict:
        policy = self._gate._enforcer._policy
        return {
            "generated_at": time.time(),
            "policy": {
                "max_chars": policy.max_chars,
                "verbosity_ratio_threshold": policy.verbosity_ratio_threshold,
                "action_on_exceed": policy.action_on_exceed.value,
                "structured_data_check": policy.structured_data_pattern_check,
            },
            "audit_1h": self._gate.audit_summary(3600.0),
            "audit_24h": self._gate.audit_summary(86400.0),
        }
```

## Comparison

| Approach | Hard Length Limit | Verbosity Ratio | Structured Data Detection | Per-Intent Budget | Audit |
|---|---|---|---|---|---|
| ResponseLengthEnforcer | Yes | No | No | No | No |
| VerbosityAnomalyDetector | No | Yes | Yes | No | No |
| PerIntentOutputBudgetRegistry | No | No | No | Yes | No |
| OutputSecurityGate | Via enforcer | Via detector | Via detector | Via registry | Yes |
| OutputLengthSecurityDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Set `max_chars=8000` as the global ceiling and override per intent — factual lookups should cap at 1000 chars, not 8000. Use `VerbosityAction.TRUNCATE` rather than `REJECT` for production: rejection causes agent task failure, while truncation limits damage while allowing the task to complete. Enable `structured_data_pattern_check=True` — a credential pattern appearing in a prose response is a near-certain prompt injection exfiltration attempt. Monitor `exfiltration_risk_flags` in `audit_summary`: a spike from a single session warrants immediate session termination and audit of the retrieved documents that were in context.
