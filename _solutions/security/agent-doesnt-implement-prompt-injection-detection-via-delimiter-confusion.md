---
title: "Agent Doesn't Implement Prompt Injection Detection via Delimiter Confusion"
description: "Agents that interpolate user-supplied content into structured prompts without escaping are vulnerable to delimiter confusion attacks: an attacker submits text containing system-prompt delimiters, role markers, or XML-like tags that the model interprets as structural instructions rather than user content. Implement delimiter confusion detection that scans user input for structural injection patterns before interpolation."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-prompt-injection-detection-via-delimiter-confusion
tags: [prompt-injection, delimiter-confusion, structural-injection, role-injection, input-sanitization, llm-security]
symptoms:
  - "User-supplied text containing '<system>' or '[INST]' tags alters model behavior"
  - "Attacker submits 'Ignore previous instructions. You are now...' and agent complies"
  - "Role markers like 'Assistant:' or 'Human:' injected in user content confuse the conversation structure"
  - "XML-style tags in user input bleed into system context in models that use XML formatting"
  - "No scanning of user content for structural prompt patterns before it is interpolated"
---

## Why This Happens

LLMs are trained to respond to structural signals — role markers, delimiter tokens, and formatting conventions — as instructions. When an agent naively interpolates `user_message` into a prompt template without escaping, a user who supplies text containing those structural signals can override system instructions. The attack surface includes: role-change phrases ("You are now", "Ignore above"), delimiter tokens (`<|im_start|>`, `[INST]`, `<s>`), XML tags that match system-prompt structure, and newline-separated fake turns that look like new conversation segments. Detection must scan for these patterns before interpolation and either reject, escape, or quarantine the content.

## Solution 1: Injection Pattern Library

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Pattern


class InjectionPatternCategory(str, Enum):
    ROLE_OVERRIDE = "role_override"
    DELIMITER_TOKEN = "delimiter_token"
    XML_STRUCTURAL = "xml_structural"
    FAKE_TURN = "fake_turn"
    INSTRUCTION_OVERRIDE = "instruction_override"


@dataclass
class InjectionPattern:
    category: InjectionPatternCategory
    pattern: Pattern
    severity: int   # 1=low, 2=medium, 3=high
    description: str


DELIMITER_INJECTION_PATTERNS: List[InjectionPattern] = [
    InjectionPattern(
        category=InjectionPatternCategory.INSTRUCTION_OVERRIDE,
        pattern=re.compile(
            r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|context)",
            re.IGNORECASE,
        ),
        severity=3,
        description="Classic instruction override phrase",
    ),
    InjectionPattern(
        category=InjectionPatternCategory.ROLE_OVERRIDE,
        pattern=re.compile(
            r"you\s+are\s+now\s+(a\s+)?(new|different|another|an?\s+AI|an?\s+assistant)",
            re.IGNORECASE,
        ),
        severity=3,
        description="Role reassignment attempt",
    ),
    InjectionPattern(
        category=InjectionPatternCategory.DELIMITER_TOKEN,
        pattern=re.compile(
            r"(<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]|<s>|</s>|<<SYS>>|<</SYS>>)",
            re.IGNORECASE,
        ),
        severity=3,
        description="Model-specific delimiter token",
    ),
    InjectionPattern(
        category=InjectionPatternCategory.XML_STRUCTURAL,
        pattern=re.compile(
            r"<(system|assistant|user|human|instruction|prompt|context)\b[^>]*>",
            re.IGNORECASE,
        ),
        severity=2,
        description="XML structural tag matching prompt format",
    ),
    InjectionPattern(
        category=InjectionPatternCategory.FAKE_TURN,
        pattern=re.compile(
            r"(\n|\A)(Human|User|Assistant|System|AI)\s*:\s*\S",
            re.IGNORECASE,
        ),
        severity=2,
        description="Fake conversation turn marker",
    ),
    InjectionPattern(
        category=InjectionPatternCategory.ROLE_OVERRIDE,
        pattern=re.compile(
            r"disregard\s+(your\s+)?(previous|prior|earlier|initial)\s+(instructions?|guidelines?|rules?|constraints?)",
            re.IGNORECASE,
        ),
        severity=3,
        description="Instruction disregard directive",
    ),
    InjectionPattern(
        category=InjectionPatternCategory.INSTRUCTION_OVERRIDE,
        pattern=re.compile(
            r"(new\s+instructions?|updated?\s+instructions?|actual\s+instructions?)\s*:",
            re.IGNORECASE,
        ),
        severity=2,
        description="Fake instruction header",
    ),
]
```

## Solution 2: Delimiter Confusion Scanner

```python
from dataclasses import dataclass, field
from typing import List


@dataclass
class InjectionMatch:
    category: InjectionPatternCategory
    pattern_description: str
    matched_text: str
    position: int
    severity: int


@dataclass
class ScanResult:
    input_text: str
    matches: List[InjectionMatch]
    max_severity: int
    blocked: bool

    @property
    def is_clean(self) -> bool:
        return len(self.matches) == 0


class DelimiterConfusionScanner:
    """
    Scans user-supplied text for prompt injection patterns using
    the DELIMITER_INJECTION_PATTERNS library.
    """

    def __init__(
        self,
        patterns: List[InjectionPattern] = None,
        block_severity_threshold: int = 2,
    ):
        self._patterns = patterns or DELIMITER_INJECTION_PATTERNS
        self._threshold = block_severity_threshold

    def scan(self, text: str) -> ScanResult:
        matches: List[InjectionMatch] = []
        for pattern_def in self._patterns:
            for m in pattern_def.pattern.finditer(text):
                matches.append(InjectionMatch(
                    category=pattern_def.category,
                    pattern_description=pattern_def.description,
                    matched_text=m.group()[:100],
                    position=m.start(),
                    severity=pattern_def.severity,
                ))

        max_severity = max((m.severity for m in matches), default=0)
        blocked = max_severity >= self._threshold

        return ScanResult(
            input_text=text,
            matches=matches,
            max_severity=max_severity,
            blocked=blocked,
        )
```

## Solution 3: Content Escaper

```python
import re


class PromptContentEscaper:
    """
    Escapes structural signals in user content so they are treated
    as literal text rather than prompt directives when interpolated.
    Applies when content is flagged as suspicious but not blocked.
    """

    # Tags to neutralize by inserting zero-width space
    _XML_TAG_RE = re.compile(r"<(/?)(\w+)([^>]*)>")
    _TURN_MARKER_RE = re.compile(
        r"^(Human|User|Assistant|System|AI)\s*:", re.IGNORECASE | re.MULTILINE
    )
    _DELIMITER_TOKEN_RE = re.compile(
        r"(<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]|<s>|</s>|<<SYS>>|<</SYS>>)",
        re.IGNORECASE,
    )

    @classmethod
    def escape(cls, text: str) -> str:
        # Neutralize model delimiter tokens
        text = cls._DELIMITER_TOKEN_RE.sub(
            lambda m: m.group().replace("<", "\u200b<").replace("[", "\u200b["),
            text,
        )
        # Neutralize XML structural tags
        text = cls._XML_TAG_RE.sub(
            lambda m: f"<{m.group(1)}{m.group(2)}\u200b{m.group(3)}>",
            text,
        )
        # Neutralize fake turn markers
        text = cls._TURN_MARKER_RE.sub(
            lambda m: m.group().replace(":", "\u200b:"),
            text,
        )
        return text
```

## Solution 4: Injection-Resistant Input Processor

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class InputDisposition(str, Enum):
    ALLOW = "allow"
    ESCAPE_AND_ALLOW = "escape_and_allow"
    BLOCK = "block"


@dataclass
class ProcessedInput:
    original: str
    processed: str
    disposition: InputDisposition
    scan_result: ScanResult
    block_reason: str = ""


class InjectionResistantInputProcessor:
    """
    Combines scanning and escaping into a single processing step.
    High-severity matches are blocked; medium-severity matches are
    escaped; clean inputs pass through unchanged.
    """

    def __init__(
        self,
        scanner: DelimiterConfusionScanner,
        escaper: PromptContentEscaper,
        block_threshold: int = 3,
        escape_threshold: int = 2,
    ):
        self._scanner = scanner
        self._escaper = escaper
        self._block_threshold = block_threshold
        self._escape_threshold = escape_threshold

    def process(self, text: str) -> ProcessedInput:
        result = self._scanner.scan(text)

        if result.max_severity >= self._block_threshold:
            return ProcessedInput(
                original=text,
                processed="",
                disposition=InputDisposition.BLOCK,
                scan_result=result,
                block_reason=f"Severity {result.max_severity} injection pattern detected: "
                             + ", ".join(m.pattern_description for m in result.matches[:3]),
            )

        if result.max_severity >= self._escape_threshold:
            escaped = self._escaper.escape(text)
            return ProcessedInput(
                original=text,
                processed=escaped,
                disposition=InputDisposition.ESCAPE_AND_ALLOW,
                scan_result=result,
            )

        return ProcessedInput(
            original=text,
            processed=text,
            disposition=InputDisposition.ALLOW,
            scan_result=result,
        )
```

## Solution 5: Injection Attempt Audit Logger

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List


class InjectionAttemptAuditLogger:
    """
    Records blocked and escaped injection attempts for security
    review. Surfaces repeat offenders and pattern frequencies.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, processed: ProcessedInput, session_id: str = "") -> None:
        if processed.disposition == InputDisposition.ALLOW:
            return
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "session_id": session_id,
                "disposition": processed.disposition.value,
                "max_severity": processed.scan_result.max_severity,
                "categories": list({m.category.value for m in processed.scan_result.matches}),
                "block_reason": processed.block_reason,
                "input_prefix": processed.original[:80],
            })
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        category_counts: dict = {}
        for r in recent:
            for cat in r["categories"]:
                category_counts[cat] = category_counts.get(cat, 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_flagged": len(recent),
            "blocked": sum(1 for r in recent if r["disposition"] == "block"),
            "escaped": sum(1 for r in recent if r["disposition"] == "escape_and_allow"),
            "by_category": category_counts,
        }
```

## Solution 6: Injection Detection Dashboard

```python
import time


class DelimiterInjectionDetectionDashboard:
    """
    Combines audit summary and live disposition stats into a
    single operational security report.
    """

    def __init__(
        self,
        processor: InjectionResistantInputProcessor,
        logger: InjectionAttemptAuditLogger,
    ):
        self._processor = processor
        self._logger = logger
        self._processed_total = 0
        self._blocked_total = 0
        self._escaped_total = 0

    def record_disposition(self, processed: ProcessedInput) -> None:
        self._processed_total += 1
        if processed.disposition == InputDisposition.BLOCK:
            self._blocked_total += 1
        elif processed.disposition == InputDisposition.ESCAPE_AND_ALLOW:
            self._escaped_total += 1

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "lifetime": {
                "total_processed": self._processed_total,
                "blocked": self._blocked_total,
                "escaped": self._escaped_total,
                "block_rate": round(self._blocked_total / max(self._processed_total, 1), 4),
            },
            "last_hour": self._logger.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Pattern Scanning | Structural Escaping | Block/Allow/Escape | Audit Logging | Dashboard |
|---|---|---|---|---|---|
| DelimiterConfusionScanner | Yes (regex library) | No | No | No | No |
| PromptContentEscaper | No | Yes (zero-width space) | No | No | No |
| InjectionResistantInputProcessor | Via scanner | Via escaper | Yes (3-tier) | No | No |
| InjectionAttemptAuditLogger | No | No | No | Yes | No |
| DelimiterInjectionDetectionDashboard | No | No | No | No | Yes |

**Best for production**: Apply `InjectionResistantInputProcessor` to every user-supplied string before it is interpolated into any prompt — including tool arguments, conversation history entries, and retrieved document content. Use `InputDisposition.ESCAPE_AND_ALLOW` for medium-severity matches rather than blanket blocking, since legitimate technical content may contain XML tags or turn-like formatting. Set `block_threshold=3` so only high-confidence attacks are rejected outright. Monitor `by_category` in audit summaries: a spike in `delimiter_token` attempts indicates automated scanning for model-specific vulnerabilities and warrants IP-level rate limiting upstream.
