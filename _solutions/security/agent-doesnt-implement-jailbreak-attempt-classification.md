---
title: "Agent Doesn't Implement Jailbreak Attempt Classification"
description: "Agents that process user inputs without jailbreak classification are blind to adversarial prompts designed to override system instructions, elicit prohibited content, or manipulate the agent's persona. Implement jailbreak attempt classification that detects known attack patterns (role-play override, DAN-style unlocking, hypothetical framing, instruction injection) using regex pattern matching and semantic heuristics, logs attempts, and escalates persistent attackers."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-jailbreak-attempt-classification
tags: [jailbreak, adversarial-prompts, input-classification, prompt-security, attack-detection, red-teaming]
symptoms:
  - "Agent adopts alternate personas when prompted with 'pretend you are a different AI'"
  - "DAN-style prompts bypass content filters by framing requests as hypotheticals"
  - "No record of how many jailbreak attempts occur per session or per user"
  - "Instruction injection in user messages overrides system prompt constraints"
  - "Security team cannot determine whether specific users are running systematic attacks"
---

## Why This Happens

Jailbreak attacks exploit the model's instruction-following behavior by framing malicious requests in ways that compete with or override the system prompt. Without a pre-processing classification step, every input is treated as legitimate user intent and passed directly to the model. A jailbreak classifier intercepts inputs before inference and flags attempts using a combination of pattern matching (fast, high precision for known attacks) and semantic heuristics (broader coverage for novel variants). Detection alone is not sufficient — the classifier must log attempts with session context so that escalation logic can identify users running systematic campaigns.

## Solution 1: Jailbreak Pattern Library

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Pattern


class JailbreakCategory(str, Enum):
    PERSONA_OVERRIDE = "persona_override"         # "pretend you are", "act as", "you are now"
    DAN_UNLOCK = "dan_unlock"                     # "do anything now", "DAN mode", "jailbreak"
    HYPOTHETICAL_FRAMING = "hypothetical_framing" # "hypothetically", "in a story", "for fiction"
    INSTRUCTION_INJECTION = "instruction_injection"  # "ignore previous", "disregard", "new instructions"
    SYSTEM_PROMPT_LEAK = "system_prompt_leak"     # "show your system prompt", "print your instructions"
    AUTHORITY_SPOOFING = "authority_spoofing"     # "I am your developer", "admin override"
    ENCODING_BYPASS = "encoding_bypass"           # base64/hex encoded instructions


@dataclass
class JailbreakPattern:
    category: JailbreakCategory
    pattern: Pattern
    severity: int = 1    # 1=low, 2=medium, 3=high
    description: str = ""


def default_jailbreak_patterns() -> List[JailbreakPattern]:
    def p(category, regex, severity, description):
        return JailbreakPattern(
            category=category,
            pattern=re.compile(regex, re.IGNORECASE | re.DOTALL),
            severity=severity,
            description=description,
        )

    return [
        p(JailbreakCategory.PERSONA_OVERRIDE,
          r"\b(pretend|act|roleplay|imagine|simulate)\b.{0,30}\b(you are|you're|as if you)\b",
          2, "Role-play persona override"),
        p(JailbreakCategory.PERSONA_OVERRIDE,
          r"\byou are now\b.{0,50}\b(without|no|ignore|bypass)\b.{0,30}\brestrictions?\b",
          3, "Unrestricted persona assignment"),
        p(JailbreakCategory.DAN_UNLOCK,
          r"\bDAN\b|\bdo anything now\b|\bjailbreak\b|\bunrestricted mode\b",
          3, "DAN-style unlock attempt"),
        p(JailbreakCategory.DAN_UNLOCK,
          r"\benable (developer|dev|god|admin|debug|unsafe) mode\b",
          2, "Mode unlock attempt"),
        p(JailbreakCategory.HYPOTHETICAL_FRAMING,
          r"\b(hypothetically|in a story|for (fiction|a novel|a movie)|if you could)\b.{0,80}\b(how (to|would|do)|provide|tell me|explain)\b",
          1, "Hypothetical framing bypass"),
        p(JailbreakCategory.INSTRUCTION_INJECTION,
          r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}\b(previous|prior|above|system|original)\b.{0,30}\b(instructions?|prompt|rules?|constraints?|guidelines?)\b",
          3, "Direct instruction injection"),
        p(JailbreakCategory.INSTRUCTION_INJECTION,
          r"(\[INST\]|<\|system\|>|<system>|###\s*System|SYSTEM:).{0,200}(ignore|bypass|override)",
          3, "System marker injection"),
        p(JailbreakCategory.SYSTEM_PROMPT_LEAK,
          r"\b(show|print|reveal|display|output|repeat|what (is|are))\b.{0,40}\b(your (system prompt|instructions?|rules?|guidelines?|prompt))\b",
          2, "System prompt extraction attempt"),
        p(JailbreakCategory.AUTHORITY_SPOOFING,
          r"\b(i am|i'm).{0,20}\b(your (developer|creator|trainer|admin|operator|owner))\b",
          2, "Authority spoofing"),
        p(JailbreakCategory.ENCODING_BYPASS,
          r"(base64|hex|rot13|caesar|encode|decode).{0,50}(instruction|command|prompt|request)",
          2, "Encoding bypass attempt"),
    ]
```

## Solution 2: Jailbreak Classifier

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class JailbreakClassificationResult:
    input_text: str
    is_jailbreak: bool
    confidence: float          # 0.0–1.0, based on pattern severity
    categories: List[str]
    matched_patterns: List[str]
    severity: int              # max severity among matched patterns
    classified_at: float = field(default_factory=time.time)


class JailbreakClassifier:
    """
    Classifies user input for jailbreak attempt patterns.
    Uses regex pattern matching with severity-weighted confidence scoring.
    """

    SEVERITY_CONFIDENCE = {1: 0.5, 2: 0.75, 3: 0.95}

    def __init__(self, patterns: List[JailbreakPattern]):
        self._patterns = patterns

    def classify(self, text: str) -> JailbreakClassificationResult:
        matched_categories = set()
        matched_descriptions = []
        max_severity = 0

        for pattern in self._patterns:
            if pattern.pattern.search(text):
                matched_categories.add(pattern.category.value)
                matched_descriptions.append(pattern.description)
                max_severity = max(max_severity, pattern.severity)

        is_jailbreak = max_severity > 0
        confidence = self.SEVERITY_CONFIDENCE.get(max_severity, 0.0) if is_jailbreak else 0.0

        return JailbreakClassificationResult(
            input_text=text[:200],
            is_jailbreak=is_jailbreak,
            confidence=confidence,
            categories=sorted(matched_categories),
            matched_patterns=matched_descriptions,
            severity=max_severity,
        )
```

## Solution 3: Heuristic Supplement

```python
import math
import re


class JailbreakHeuristicScorer:
    """
    Supplements pattern matching with heuristic signals that correlate
    with jailbreak attempts even when no specific pattern matches.
    """

    def score(self, text: str) -> float:
        """Returns a 0.0–1.0 suspicion score based on heuristics."""
        score = 0.0
        signals = 0

        # Excessive instruction-like capitalization
        caps_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
        if caps_ratio > 0.3:
            score += 0.2
            signals += 1

        # Unusually long single message (padding attack signal)
        if len(text) > 2000:
            score += 0.15
            signals += 1

        # Multiple imperative verbs (command injection style)
        imperative_count = len(re.findall(
            r"\b(ignore|forget|disregard|override|bypass|pretend|act|simulate|output|print|reveal)\b",
            text, re.IGNORECASE
        ))
        if imperative_count >= 3:
            score += min(imperative_count * 0.1, 0.4)
            signals += 1

        # Quoted instruction blocks
        quoted_instructions = len(re.findall(r'["\'].*?(instruction|prompt|rule|guideline).*?["\']', text, re.IGNORECASE))
        if quoted_instructions:
            score += 0.2
            signals += 1

        # Nested role-play framing
        nesting = len(re.findall(r"\b(within|inside|nested|embedded)\b.{0,20}\b(scenario|story|roleplay|simulation)\b", text, re.IGNORECASE))
        if nesting:
            score += 0.25
            signals += 1

        return round(min(score, 1.0), 3)
```

## Solution 4: Session Jailbreak Tracker

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class SessionJailbreakTracker:
    """
    Tracks jailbreak attempt counts per session and per user.
    Escalates sessions that exceed attempt thresholds.
    """

    def __init__(
        self,
        attempt_escalation_threshold: int = 3,
        window_seconds: float = 3600.0,
    ):
        self._threshold = attempt_escalation_threshold
        self._window = window_seconds
        self._attempts: Dict[str, List[float]] = defaultdict(list)
        self._escalated: Dict[str, float] = {}
        self._lock = Lock()

    def record(self, session_id: str, result: JailbreakClassificationResult) -> bool:
        """
        Records a jailbreak attempt. Returns True if the session
        should be escalated based on attempt frequency.
        """
        if not result.is_jailbreak:
            return False
        now = time.time()
        with self._lock:
            self._attempts[session_id].append(now)
            cutoff = now - self._window
            self._attempts[session_id] = [
                t for t in self._attempts[session_id] if t >= cutoff
            ]
            count = len(self._attempts[session_id])
            if count >= self._threshold and session_id not in self._escalated:
                self._escalated[session_id] = now
                return True
        return False

    def is_escalated(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._escalated

    def attempt_count(self, session_id: str, window_seconds: float = None) -> int:
        window = window_seconds or self._window
        cutoff = time.time() - window
        with self._lock:
            return sum(1 for t in self._attempts.get(session_id, []) if t >= cutoff)

    def escalated_sessions(self) -> List[str]:
        with self._lock:
            return list(self._escalated.keys())
```

## Solution 5: Jailbreak Audit Logger

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import List


class JailbreakAuditLogger:
    """
    Appends jailbreak detection events to a JSONL audit log
    for forensic analysis and compliance reporting.
    """

    def __init__(self, log_path: str = "/tmp/jailbreak_audit.jsonl"):
        self._path = Path(log_path)
        self._lock = Lock()
        self._total_logged = 0

    def log(
        self,
        session_id: str,
        user_id: str,
        result: JailbreakClassificationResult,
        escalated: bool = False,
    ) -> None:
        if not result.is_jailbreak:
            return
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "user_id": user_id,
            "severity": result.severity,
            "confidence": result.confidence,
            "categories": result.categories,
            "patterns": result.matched_patterns,
            "input_preview": result.input_text[:100],
            "escalated": escalated,
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")
            self._total_logged += 1

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        records = []
        if not self._path.exists():
            return {"window_seconds": window_seconds, "attempts": 0}
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    if r.get("ts", 0) >= cutoff:
                        records.append(r)
                except json.JSONDecodeError:
                    continue
        by_category: dict = {}
        for r in records:
            for cat in r.get("categories", []):
                by_category[cat] = by_category.get(cat, 0) + 1
        return {
            "window_seconds": window_seconds,
            "attempts": len(records),
            "escalated": sum(1 for r in records if r.get("escalated")),
            "by_category": by_category,
            "high_severity": sum(1 for r in records if r.get("severity", 0) >= 3),
        }
```

## Solution 6: Jailbreak Detection Pipeline

```python
from typing import Optional


class JailbreakDetectionPipeline:
    """
    Combines pattern classification, heuristic scoring, session tracking,
    and audit logging into a single callable guard for agent inputs.
    """

    HEURISTIC_THRESHOLD = 0.5

    def __init__(
        self,
        classifier: JailbreakClassifier,
        heuristic: JailbreakHeuristicScorer,
        tracker: SessionJailbreakTracker,
        logger: JailbreakAuditLogger,
    ):
        self._classifier = classifier
        self._heuristic = heuristic
        self._tracker = tracker
        self._logger = logger

    def inspect(
        self,
        text: str,
        session_id: str,
        user_id: str = "",
    ) -> dict:
        result = self._classifier.classify(text)
        heuristic_score = self._heuristic.score(text)

        # Upgrade to jailbreak if heuristic is strong even without pattern match
        if not result.is_jailbreak and heuristic_score >= self.HEURISTIC_THRESHOLD:
            result.is_jailbreak = True
            result.confidence = heuristic_score
            result.categories = ["heuristic"]
            result.severity = 1

        escalated = self._tracker.record(session_id, result)
        self._logger.log(session_id, user_id, result, escalated)

        return {
            "is_jailbreak": result.is_jailbreak,
            "severity": result.severity,
            "confidence": result.confidence,
            "categories": result.categories,
            "heuristic_score": heuristic_score,
            "session_escalated": self._tracker.is_escalated(session_id),
            "session_attempt_count": self._tracker.attempt_count(session_id),
            "block_recommended": result.severity >= 3 or self._tracker.is_escalated(session_id),
        }
```

## Comparison

| Approach | Pattern Matching | Heuristic Scoring | Session Tracking | Audit Logging | Full Pipeline |
|---|---|---|---|---|---|
| JailbreakClassifier | Yes (regex, 10 patterns) | No | No | No | No |
| JailbreakHeuristicScorer | No | Yes (5 signals) | No | No | No |
| SessionJailbreakTracker | No | No | Yes (escalation) | No | No |
| JailbreakAuditLogger | No | No | No | Yes (JSONL) | No |
| JailbreakDetectionPipeline | Via classifier | Via heuristic | Via tracker | Via logger | Yes |

**Best for production**: Run the pipeline on every user message before passing to the model — latency is microseconds for pattern matching and under 1ms for heuristic scoring. Block immediately on severity 3 patterns (instruction injection, DAN unlock) without model inference. For severity 1–2, let the model handle the request but log the detection and increment the session counter. Escalate sessions after 3 attempts within an hour — at that point the user is running a systematic campaign, not making an accidental phrasing. Export `JailbreakAuditLogger.summary()` hourly to a security dashboard; a spike in `persona_override` attempts often precedes a novel attack wave and warrants pattern library updates.
