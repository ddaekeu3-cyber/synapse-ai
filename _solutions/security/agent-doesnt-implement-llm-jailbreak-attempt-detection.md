---
title: "Agent Doesn't Implement LLM Jailbreak Attempt Detection"
description: "Agents without jailbreak detection cannot distinguish legitimate queries from adversarial prompts designed to bypass safety guidelines, impersonate system roles, or extract training data. Implement jailbreak attempt detection that scans incoming queries for known attack patterns, scores prompt suspicion level using multiple signals, and routes high-risk prompts to a secondary review layer before processing."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-llm-jailbreak-attempt-detection
tags: [jailbreak-detection, adversarial-prompts, prompt-security, safety-bypass, attack-patterns, red-teaming]
symptoms:
  - "Users successfully extract the system prompt by asking the LLM to repeat its instructions"
  - "Role-play prompts convince the agent to ignore safety guidelines"
  - "No pre-processing layer to catch adversarial inputs before they reach the LLM"
  - "Agent responds to 'ignore all previous instructions' without any alert firing"
  - "No visibility into how often jailbreak attempts occur or which patterns are most common"
---

## Why This Happens

LLMs are trained to be helpful and follow instructions, which makes them susceptible to carefully crafted prompts that reframe the context (DAN, role-play, hypothetical scenarios), override earlier instructions ("ignore all previous instructions"), or extract internal state ("repeat the first 100 words of your system prompt"). Without a detection layer, every input goes directly to the LLM with no opportunity to flag, filter, or rate-limit adversarial traffic. Detection does not require perfect accuracy — even a probabilistic signal that triggers additional review reduces successful attacks.

## Solution 1: Jailbreak Pattern Library

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Pattern


class JailbreakCategory(str, Enum):
    INSTRUCTION_OVERRIDE = "instruction_override"   # "ignore previous instructions"
    ROLE_PLAY_BYPASS = "role_play_bypass"           # "pretend you are DAN / evil AI"
    SYSTEM_PROMPT_EXTRACTION = "system_extraction"  # "repeat your instructions"
    HYPOTHETICAL_FRAMING = "hypothetical"           # "in a fictional world where..."
    ENCODING_OBFUSCATION = "encoding"               # base64 / rot13 payload
    AUTHORITY_IMPERSONATION = "impersonation"       # "as your developer I command"
    TOKEN_SMUGGLING = "token_smuggling"             # unusual unicode / homoglyphs
    MULTI_TURN_BUILDUP = "multi_turn"               # gradual context manipulation


@dataclass
class JailbreakPattern:
    pattern_id: str
    category: JailbreakCategory
    regex: Pattern
    base_score: float   # 0.0–1.0 contribution to jailbreak probability
    description: str


JAILBREAK_PATTERNS: List[JailbreakPattern] = [
    JailbreakPattern("J001", JailbreakCategory.INSTRUCTION_OVERRIDE,
        re.compile(r"ignore (all |previous |your )?(previous |prior )?instructions?", re.IGNORECASE),
        0.85, "Classic instruction override"),
    JailbreakPattern("J002", JailbreakCategory.INSTRUCTION_OVERRIDE,
        re.compile(r"disregard (all |previous |your )?(previous |prior )?instructions?", re.IGNORECASE),
        0.85, "Disregard instruction override"),
    JailbreakPattern("J003", JailbreakCategory.INSTRUCTION_OVERRIDE,
        re.compile(r"forget (everything|what you were told|your (instructions?|guidelines?))", re.IGNORECASE),
        0.80, "Forget-based override"),
    JailbreakPattern("J004", JailbreakCategory.ROLE_PLAY_BYPASS,
        re.compile(r"\b(DAN|do anything now|jailbreak(ed)?|uncensored (ai|mode))\b", re.IGNORECASE),
        0.90, "DAN / jailbreak mode"),
    JailbreakPattern("J005", JailbreakCategory.ROLE_PLAY_BYPASS,
        re.compile(r"pretend (you are|to be) (an? )?(evil|unfiltered|unrestricted|uncensored)", re.IGNORECASE),
        0.85, "Evil AI role-play"),
    JailbreakPattern("J006", JailbreakCategory.ROLE_PLAY_BYPASS,
        re.compile(r"act as (if you have no|without any) (restrictions?|limitations?|guidelines?|filters?)", re.IGNORECASE),
        0.80, "No-restrictions role-play"),
    JailbreakPattern("J007", JailbreakCategory.SYSTEM_PROMPT_EXTRACTION,
        re.compile(r"(repeat|print|show|output|reveal|tell me) (your |the )?(system prompt|initial instructions?|first message)", re.IGNORECASE),
        0.75, "System prompt extraction"),
    JailbreakPattern("J008", JailbreakCategory.SYSTEM_PROMPT_EXTRACTION,
        re.compile(r"what (were you |are you )?(told|instructed|programmed) to", re.IGNORECASE),
        0.60, "Indirect system prompt probing"),
    JailbreakPattern("J009", JailbreakCategory.HYPOTHETICAL_FRAMING,
        re.compile(r"(in a )?hypothetical(ly)? (world|scenario|situation) where (you|ai) (can|could|are allowed)", re.IGNORECASE),
        0.65, "Hypothetical framing"),
    JailbreakPattern("J010", JailbreakCategory.AUTHORITY_IMPERSONATION,
        re.compile(r"(as your (developer|creator|owner|admin|openai|anthropic)|i (am|am your) (developer|creator|admin))", re.IGNORECASE),
        0.80, "Authority impersonation"),
    JailbreakPattern("J011", JailbreakCategory.ENCODING_OBFUSCATION,
        re.compile(r"(decode (this|the following)|base64|rot13|hex.?encoded)", re.IGNORECASE),
        0.55, "Encoding obfuscation"),
    JailbreakPattern("J012", JailbreakCategory.TOKEN_SMUGGLING,
        re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]", re.UNICODE),
        0.70, "Zero-width character injection"),
]
```

## Solution 2: Jailbreak Scorer

```python
import math
from typing import List, Optional, Tuple


@dataclass
class JailbreakScanResult:
    input_text: str
    jailbreak_probability: float    # 0.0–1.0
    matched_patterns: List[JailbreakPattern]
    categories_detected: List[JailbreakCategory]
    risk_level: str                 # "low" | "medium" | "high" | "critical"
    recommendation: str             # "allow" | "review" | "block"

    def is_suspicious(self) -> bool:
        return self.jailbreak_probability >= 0.40

    def should_block(self) -> bool:
        return self.jailbreak_probability >= 0.80


class JailbreakScorer:
    """
    Scans input text against all known jailbreak patterns and computes
    a composite jailbreak probability using noisy-or combination.
    """

    def __init__(
        self,
        patterns: List[JailbreakPattern] = None,
        block_threshold: float = 0.80,
        review_threshold: float = 0.40,
    ) -> None:
        self._patterns = patterns or JAILBREAK_PATTERNS
        self._block = block_threshold
        self._review = review_threshold

    def score(self, text: str) -> JailbreakScanResult:
        matched = []
        for pattern in self._patterns:
            if pattern.regex.search(text):
                matched.append(pattern)

        # Noisy-OR combination: P(any match) = 1 - product(1 - score_i)
        prob = 1.0 - math.prod(1.0 - p.base_score for p in matched) if matched else 0.0
        prob = round(min(prob, 1.0), 4)

        categories = list({p.category for p in matched})

        if prob >= self._block:
            risk = "critical"
            rec = "block"
        elif prob >= self._review:
            risk = "high" if prob >= 0.60 else "medium"
            rec = "review"
        else:
            risk = "low"
            rec = "allow"

        return JailbreakScanResult(
            input_text=text[:200],
            jailbreak_probability=prob,
            matched_patterns=matched,
            categories_detected=categories,
            risk_level=risk,
            recommendation=rec,
        )
```

## Solution 3: Contextual Escalation Scorer

```python
from typing import List


class ContextualEscalationScorer:
    """
    Analyzes multi-turn patterns for gradual jailbreak buildup.
    Scores increase when a session shows repeated low-probability
    attempts that together suggest systematic probing.
    """

    def __init__(
        self,
        session_window: int = 10,
        escalation_threshold: float = 0.30,
        escalation_multiplier: float = 1.5,
    ) -> None:
        self._window = session_window
        self._threshold = escalation_threshold
        self._multiplier = escalation_multiplier
        self._session_scores: dict = {}

    def record_and_adjust(
        self,
        session_id: str,
        base_result: JailbreakScanResult,
    ) -> JailbreakScanResult:
        """
        Adjusts jailbreak probability upward if the session has a history
        of suspicious prompts, even if each individual score is low.
        """
        if session_id not in self._session_scores:
            self._session_scores[session_id] = []

        history = self._session_scores[session_id]
        history.append(base_result.jailbreak_probability)
        if len(history) > self._window:
            history.pop(0)

        suspicious_turns = sum(1 for s in history if s >= self._threshold)
        if suspicious_turns >= 3:
            adjusted_prob = min(
                1.0,
                base_result.jailbreak_probability * self._multiplier,
            )
            # Return adjusted result with same metadata
            return JailbreakScanResult(
                input_text=base_result.input_text,
                jailbreak_probability=round(adjusted_prob, 4),
                matched_patterns=base_result.matched_patterns,
                categories_detected=base_result.categories_detected + [JailbreakCategory.MULTI_TURN_BUILDUP],
                risk_level="high" if adjusted_prob >= 0.60 else base_result.risk_level,
                recommendation="review" if adjusted_prob >= 0.40 else base_result.recommendation,
            )

        return base_result
```

## Solution 4: Jailbreak Detection Gate

```python
import time
from typing import Callable, Optional


class JailbreakDetectionGate:
    """
    Intercepts incoming queries, scans them, and decides whether to
    allow, route to review, or block before the LLM sees them.
    """

    def __init__(
        self,
        scorer: JailbreakScorer,
        contextual_scorer: ContextualEscalationScorer,
        review_handler: Optional[Callable[[JailbreakScanResult, str], None]] = None,
    ) -> None:
        self._scorer = scorer
        self._contextual = contextual_scorer
        self._review_handler = review_handler
        self._scan_log: list = []

    def evaluate(self, query: str, session_id: str) -> JailbreakScanResult:
        base = self._scorer.score(query)
        result = self._contextual.record_and_adjust(session_id, base)

        self._scan_log.append({
            "session_id": session_id,
            "probability": result.jailbreak_probability,
            "recommendation": result.recommendation,
            "categories": [c.value for c in result.categories_detected],
            "scanned_at": time.time(),
        })

        if result.recommendation == "review" and self._review_handler:
            try:
                self._review_handler(result, session_id)
            except Exception:
                pass

        return result

    def should_proceed(self, result: JailbreakScanResult) -> bool:
        return result.recommendation != "block"

    def scan_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._scan_log if e["scanned_at"] >= cutoff]
        blocked = sum(1 for e in recent if e["recommendation"] == "block")
        reviewed = sum(1 for e in recent if e["recommendation"] == "review")

        return {
            "total_scanned": len(recent),
            "blocked": blocked,
            "reviewed": reviewed,
            "allowed": len(recent) - blocked - reviewed,
            "block_rate": round(blocked / max(len(recent), 1), 4),
        }
```

## Solution 5: Jailbreak Audit Reporter

```python
import time
from collections import Counter
from typing import List


class JailbreakAuditReporter:
    """
    Analyzes scan history to identify most common attack categories,
    sessions with repeated attempts, and trend changes.
    """

    def __init__(self, gate: JailbreakDetectionGate) -> None:
        self._gate = gate

    def report(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._gate._scan_log if e["scanned_at"] >= cutoff]

        if not recent:
            return {"generated_at": time.time(), "scans": 0}

        cat_counter: Counter = Counter()
        for e in recent:
            for cat in e.get("categories", []):
                cat_counter[cat] += 1

        session_attacks = Counter(
            e["session_id"] for e in recent
            if e["recommendation"] in ("block", "review")
        )

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "total_scans": len(recent),
            "blocked": sum(1 for e in recent if e["recommendation"] == "block"),
            "reviewed": sum(1 for e in recent if e["recommendation"] == "review"),
            "top_categories": dict(cat_counter.most_common(5)),
            "repeat_offender_sessions": dict(session_attacks.most_common(5)),
        }
```

## Solution 6: Jailbreak Detection Dashboard

```python
import time


class JailbreakDetectionDashboard:
    """
    Combines scan gate stats and audit report into a security operational view.
    """

    def __init__(
        self,
        gate: JailbreakDetectionGate,
        reporter: JailbreakAuditReporter,
    ) -> None:
        self._gate = gate
        self._reporter = reporter

    def render(self) -> dict:
        hourly = self._gate.scan_summary(3600.0)
        daily = self._reporter.report(86400.0)
        alerts = []

        if hourly["block_rate"] > 0.05:
            alerts.append({
                "type": "elevated_block_rate",
                "block_rate": hourly["block_rate"],
                "severity": "warning",
                "message": f"Jailbreak block rate {hourly['block_rate']*100:.1f}% exceeds 5% — active attack possible",
            })

        return {
            "generated_at": time.time(),
            "hourly_summary": hourly,
            "daily_report": daily,
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Pattern Matching | Probability Scoring | Multi-Turn Context | Blocking Gate | Audit Trail |
|---|---|---|---|---|---|
| JailbreakScorer | Yes (12 patterns) | Yes (noisy-OR) | No | No | No |
| ContextualEscalationScorer | No | Via scorer | Yes | No | No |
| JailbreakDetectionGate | Via scorer | Via scorer | Via contextual | Yes | Partial |
| JailbreakAuditReporter | No | No | No | No | Yes |
| JailbreakDetectionDashboard | No | No | No | No | Yes |

**Best for production**: Run jailbreak detection on every user turn before the query reaches the LLM — the latency cost (< 1ms for regex scanning) is negligible compared to the damage from a successful bypass. Set `block_threshold=0.80` as a hard block and `review_threshold=0.40` for soft escalation — at 0.40 the query still proceeds but triggers logging and optional human review. Tune `base_score` values per pattern based on your false positive rate from real traffic — some patterns like J008 ("what were you told") have high false positive rates in legitimate use cases and may need lower scores in your context.
