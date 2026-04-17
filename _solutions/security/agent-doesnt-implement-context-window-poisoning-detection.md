---
title: "Agent Doesn't Implement Context Window Poisoning Detection"
description: "Agents that assemble context from multiple sources without integrity checks are vulnerable to context window poisoning: an attacker injects adversarial instructions into a retrieved document, tool result, or memory entry that then silently overrides the agent's original system prompt. Implement poisoning detection that scans injected content for instruction-like patterns, role-override attempts, and prompt boundary escape sequences before they reach the LLM."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-context-window-poisoning-detection
tags: [context-poisoning, prompt-injection, context-integrity, instruction-injection, role-override, adversarial-input]
symptoms:
  - "Agent ignores its system prompt after processing a retrieved document"
  - "Tool results containing instructions like 'ignore previous instructions' alter agent behavior"
  - "Memory entries retrieved from vector stores contain injected directives that execute"
  - "Agent suddenly switches persona or discloses restricted information mid-session"
  - "No scanning of injected context for instruction-like patterns before LLM sees them"
---

## Why This Happens

RAG pipelines, tool outputs, and memory retrievals all inject third-party content directly into the LLM context. If that content contains instruction-like text — "ignore previous instructions", role-override attempts, or fake system prompt delimiters — the LLM may follow them, effectively allowing the attacker to hijack the agent's behavior without ever touching the system prompt directly. Detection requires scanning injected content for patterns that mimic prompt structure: imperative verbs targeting the model, role declarations, explicit override phrases, and prompt boundary tokens that could confuse the parser.

## Solution 1: Poisoning Pattern Library

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Pattern


class PoisoningCategory(str, Enum):
    OVERRIDE_INSTRUCTION = "override_instruction"   # "ignore previous instructions"
    ROLE_INJECTION = "role_injection"               # "you are now..."
    PROMPT_BOUNDARY_ESCAPE = "prompt_boundary_escape"  # fake delimiters
    GOAL_SUBSTITUTION = "goal_substitution"         # "your new task is..."
    DISCLOSURE_ELICITATION = "disclosure_elicitation"  # "reveal your system prompt"
    JAILBREAK_EMBEDDED = "jailbreak_embedded"       # DAN / AIM patterns in content


@dataclass
class PoisoningPattern:
    category: PoisoningCategory
    pattern: str           # regex
    severity: float        # 0.0–1.0
    description: str

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, re.IGNORECASE | re.DOTALL)


DEFAULT_POISONING_PATTERNS: List[PoisoningPattern] = [
    PoisoningPattern(
        category=PoisoningCategory.OVERRIDE_INSTRUCTION,
        pattern=r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|directives?)",
        severity=0.95,
        description="Classic override phrase",
    ),
    PoisoningPattern(
        category=PoisoningCategory.OVERRIDE_INSTRUCTION,
        pattern=r"disregard\s+(your\s+)?(previous|prior|earlier|all)\s+(instructions?|guidelines?|rules?)",
        severity=0.90,
        description="Disregard variant",
    ),
    PoisoningPattern(
        category=PoisoningCategory.ROLE_INJECTION,
        pattern=r"you\s+are\s+(now\s+)?(a|an|the)\s+\w+(\s+\w+){0,5}\s+(assistant|model|ai|bot|agent)",
        severity=0.85,
        description="Role redefinition attempt",
    ),
    PoisoningPattern(
        category=PoisoningCategory.ROLE_INJECTION,
        pattern=r"(act|behave|respond|pretend)\s+as\s+(if\s+)?(you\s+(are|were)|a|an)",
        severity=0.80,
        description="Persona substitution",
    ),
    PoisoningPattern(
        category=PoisoningCategory.PROMPT_BOUNDARY_ESCAPE,
        pattern=r"<\s*(system|assistant|user|human|instruction)\s*>",
        severity=0.90,
        description="Fake prompt boundary tag",
    ),
    PoisoningPattern(
        category=PoisoningCategory.PROMPT_BOUNDARY_ESCAPE,
        pattern=r"\[\s*(SYSTEM|INST|SYS|HUMAN|ASSISTANT)\s*\]",
        severity=0.85,
        description="Bracket-style delimiter injection",
    ),
    PoisoningPattern(
        category=PoisoningCategory.GOAL_SUBSTITUTION,
        pattern=r"(your\s+)?(new|actual|real|true|updated)\s+(goal|task|objective|mission|purpose)\s+is",
        severity=0.88,
        description="Goal substitution attempt",
    ),
    PoisoningPattern(
        category=PoisoningCategory.DISCLOSURE_ELICITATION,
        pattern=r"(reveal|print|output|show|display|repeat|tell\s+me)\s+(your\s+)?(system\s+prompt|instructions?|configuration|rules)",
        severity=0.92,
        description="System prompt extraction attempt",
    ),
    PoisoningPattern(
        category=PoisoningCategory.JAILBREAK_EMBEDDED,
        pattern=r"\b(DAN|AIM|STAN|KEVIN|jailbreak)\b",
        severity=0.75,
        description="Known jailbreak persona embedded in content",
    ),
    PoisoningPattern(
        category=PoisoningCategory.OVERRIDE_INSTRUCTION,
        pattern=r"(from\s+now\s+on|starting\s+now|henceforth)\s*[,:]?\s*(you\s+(must|should|will|shall))",
        severity=0.82,
        description="Temporal override phrasing",
    ),
]
```

## Solution 2: Context Poisoning Scanner

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PoisoningMatch:
    category: PoisoningCategory
    pattern_description: str
    matched_text: str
    start: int
    end: int
    severity: float


@dataclass
class ContentScanResult:
    content_source: str        # e.g. "retrieval", "tool:web_search", "memory"
    is_poisoned: bool
    max_severity: float
    matches: List[PoisoningMatch]
    sanitized_content: Optional[str] = None


class ContextPoisoningScanner:
    """
    Scans injected content for adversarial instruction patterns.
    Returns a scan result and optionally a sanitized version of the content.
    """

    def __init__(
        self,
        patterns: List[PoisoningPattern],
        alert_threshold: float = 0.70,
        sanitize: bool = True,
    ):
        self._patterns = patterns
        self._threshold = alert_threshold
        self._sanitize = sanitize
        self._compiled = [(p, p.compiled()) for p in patterns]

    def scan(self, content: str, source: str = "unknown") -> ContentScanResult:
        matches: List[PoisoningMatch] = []

        for pattern, compiled in self._compiled:
            for m in compiled.finditer(content):
                matches.append(PoisoningMatch(
                    category=pattern.category,
                    pattern_description=pattern.description,
                    matched_text=m.group(0)[:120],
                    start=m.start(),
                    end=m.end(),
                    severity=pattern.severity,
                ))

        max_severity = max((m.severity for m in matches), default=0.0)
        is_poisoned = max_severity >= self._threshold

        sanitized = None
        if self._sanitize and is_poisoned:
            sanitized = self._sanitize_content(content, matches)

        return ContentScanResult(
            content_source=source,
            is_poisoned=is_poisoned,
            max_severity=round(max_severity, 4),
            matches=matches,
            sanitized_content=sanitized,
        )

    def _sanitize_content(self, content: str, matches: List[PoisoningMatch]) -> str:
        """Replace matched spans with a neutral placeholder."""
        # Build replacement list in reverse order to preserve offsets
        replacements = sorted(matches, key=lambda m: m.start, reverse=True)
        result = content
        for m in replacements:
            result = result[:m.start] + "[CONTENT_REDACTED]" + result[m.end:]
        return result
```

## Solution 3: Structural Anomaly Detector

```python
import math
import re
from dataclasses import dataclass
from typing import List


@dataclass
class StructuralAnomalyResult:
    anomalies: List[str]
    anomaly_score: float     # 0.0–1.0 aggregate


class StructuralAnomalyDetector:
    """
    Detects structural anomalies in injected content that do not match
    known attack patterns but exhibit suspicious characteristics:
    unusually high imperative verb density, abnormal instruction-to-noise ratio,
    or encoded/obfuscated text.
    """

    IMPERATIVE_VERBS = re.compile(
        r"\b(ignore|disregard|forget|override|replace|update|change|pretend|act|behave|respond|answer|output|print|reveal|tell|show)\b",
        re.IGNORECASE,
    )
    BASE64_LIKE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
    UNICODE_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")
    REPETITIVE_STRUCTURE = re.compile(r"(\b\w{3,}\b)(?:\s+\1){4,}")

    def detect(self, content: str) -> StructuralAnomalyResult:
        anomalies = []
        scores = []

        words = content.split()
        if not words:
            return StructuralAnomalyResult(anomalies=[], anomaly_score=0.0)

        # Imperative verb density
        imperative_count = len(self.IMPERATIVE_VERBS.findall(content))
        density = imperative_count / max(len(words), 1)
        if density > 0.08:
            anomalies.append(f"high_imperative_density: {density:.3f}")
            scores.append(min(density * 5, 1.0))

        # Base64/encoded blobs in otherwise natural-language content
        b64_matches = self.BASE64_LIKE.findall(content)
        if b64_matches and len(" ".join(b64_matches)) > 0.1 * len(content):
            anomalies.append(f"encoded_content_blob: {len(b64_matches)} segment(s)")
            scores.append(0.65)

        # Unicode escape sequences
        unicode_escapes = self.UNICODE_ESCAPE.findall(content)
        if len(unicode_escapes) > 5:
            anomalies.append(f"unicode_escape_sequences: {len(unicode_escapes)}")
            scores.append(0.60)

        # Repetitive word structure (padding signal)
        if self.REPETITIVE_STRUCTURE.search(content):
            anomalies.append("repetitive_word_structure")
            scores.append(0.45)

        score = max(scores, default=0.0)
        return StructuralAnomalyResult(anomalies=anomalies, anomaly_score=round(score, 4))
```

## Solution 4: Context Injection Guard

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class InjectionGuardDecision:
    allowed: bool
    source: str
    block_reason: str = ""
    scan_result: Optional[ContentScanResult] = None
    anomaly_result: Optional[StructuralAnomalyResult] = None


class ContextInjectionGuard:
    """
    Gate that all context injections must pass through before reaching the LLM.
    Combines pattern scanning and structural anomaly detection.
    Blocks, sanitizes, or passes content based on combined risk score.
    """

    def __init__(
        self,
        scanner: ContextPoisoningScanner,
        anomaly_detector: StructuralAnomalyDetector,
        block_threshold: float = 0.80,
        sanitize_threshold: float = 0.50,
    ):
        self._scanner = scanner
        self._anomaly = anomaly_detector
        self._block_threshold = block_threshold
        self._sanitize_threshold = sanitize_threshold

    def evaluate(self, content: str, source: str) -> InjectionGuardDecision:
        scan = self._scanner.scan(content, source)
        anomaly = self._anomaly.detect(content)

        combined_score = max(scan.max_severity, anomaly.anomaly_score)

        if combined_score >= self._block_threshold:
            return InjectionGuardDecision(
                allowed=False,
                source=source,
                block_reason=f"combined_score={combined_score:.3f} exceeds block threshold",
                scan_result=scan,
                anomaly_result=anomaly,
            )

        if combined_score >= self._sanitize_threshold and scan.sanitized_content:
            # Allow but replace content with sanitized version
            return InjectionGuardDecision(
                allowed=True,
                source=source,
                block_reason="sanitized",
                scan_result=scan,
                anomaly_result=anomaly,
            )

        return InjectionGuardDecision(
            allowed=True,
            source=source,
            scan_result=scan,
            anomaly_result=anomaly,
        )

    def safe_content(self, content: str, source: str) -> Optional[str]:
        """
        Returns the safe content to inject, or None if it should be blocked.
        Returns sanitized version if warranted.
        """
        decision = self.evaluate(content, source)
        if not decision.allowed:
            return None
        if decision.scan_result and decision.scan_result.sanitized_content:
            return decision.scan_result.sanitized_content
        return content
```

## Solution 5: Poisoning Incident Logger

```python
import time
from typing import List


class PoisoningIncidentLogger:
    """
    Records all detected poisoning attempts with source attribution.
    Provides a summary of attack frequency and most targeted categories.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        decision: InjectionGuardDecision,
        session_id: str = "",
        request_id: str = "",
    ) -> None:
        if decision.allowed and not decision.block_reason:
            return  # clean pass — nothing to log

        scan = decision.scan_result
        anomaly = decision.anomaly_result

        if len(self._records) >= self._max:
            self._records.pop(0)

        categories = []
        if scan:
            categories = list({m.category.value for m in scan.matches})

        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "request_id": request_id,
            "source": decision.source,
            "blocked": not decision.allowed,
            "sanitized": decision.block_reason == "sanitized",
            "max_severity": scan.max_severity if scan else 0.0,
            "anomaly_score": anomaly.anomaly_score if anomaly else 0.0,
            "categories": categories,
            "match_count": len(scan.matches) if scan else 0,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "incidents": 0}

        category_counts: dict = {}
        for r in recent:
            for cat in r["categories"]:
                category_counts[cat] = category_counts.get(cat, 0) + 1

        return {
            "window_seconds": window_seconds,
            "incidents": len(recent),
            "blocked": sum(1 for r in recent if r["blocked"]),
            "sanitized": sum(1 for r in recent if r["sanitized"]),
            "by_source": {
                src: sum(1 for r in recent if r["source"] == src)
                for src in {r["source"] for r in recent}
            },
            "by_category": category_counts,
        }
```

## Solution 6: Context Poisoning Detection Dashboard

```python
import time


class ContextPoisoningDashboard:
    """
    Combines the injection guard's decision statistics with the incident
    log to give a full operational view of poisoning attempts.
    """

    def __init__(
        self,
        guard: ContextInjectionGuard,
        incident_logger: PoisoningIncidentLogger,
    ):
        self._guard = guard
        self._logger = incident_logger
        self._total_evaluated = 0
        self._total_blocked = 0
        self._total_sanitized = 0

    def evaluate_and_record(
        self,
        content: str,
        source: str,
        session_id: str = "",
        request_id: str = "",
    ) -> InjectionGuardDecision:
        decision = self._guard.evaluate(content, source)
        self._total_evaluated += 1
        if not decision.allowed:
            self._total_blocked += 1
        elif decision.block_reason == "sanitized":
            self._total_sanitized += 1
        self._incident_logger.record(decision, session_id, request_id)
        return decision

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "lifetime_stats": {
                "evaluated": self._total_evaluated,
                "blocked": self._total_blocked,
                "sanitized": self._total_sanitized,
                "block_rate": round(
                    self._total_blocked / max(self._total_evaluated, 1), 4
                ),
            },
            "last_hour": self._logger.summary(window_seconds=3600.0),
        }

    @property
    def _incident_logger(self) -> PoisoningIncidentLogger:
        return self._logger
```

## Comparison

| Approach | Pattern Matching | Structural Analysis | Sanitization | Source Attribution | Audit Log |
|---|---|---|---|---|---|
| ContextPoisoningScanner | Yes (10 patterns) | No | Yes (span replace) | Yes | No |
| StructuralAnomalyDetector | No | Yes (4 signals) | No | No | No |
| ContextInjectionGuard | Via scanner | Via anomaly | Via scanner | Yes | No |
| PoisoningIncidentLogger | No | No | No | Yes | Yes |
| ContextPoisoningDashboard | Via guard | Via guard | Via guard | Yes | Yes |

**Best for production**: Place `ContextInjectionGuard.safe_content()` as the mandatory gate between any retrieval or tool result and the LLM context builder — no content should bypass it. Set `block_threshold=0.85` for automated blocking and `sanitize_threshold=0.55` to strip high-confidence matches while preserving low-risk content. Monitor `PoisoningIncidentLogger.summary()` by source: if retrieval sources produce consistently higher incident rates than tool results, the retrieval corpus may itself be compromised and should be re-scanned. Add new `PoisoningPattern` entries as attack patterns evolve — the library is the primary lever; the infrastructure around it is stable.
