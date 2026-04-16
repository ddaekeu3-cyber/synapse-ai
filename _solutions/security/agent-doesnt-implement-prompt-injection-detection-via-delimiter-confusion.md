---
title: "Agent Doesn't Implement Prompt Injection Detection via Delimiter Confusion"
description: "Agents that interpolate user-supplied content directly into structured prompts — between role delimiters, XML tags, or JSON fields — are vulnerable to delimiter confusion attacks: an attacker who knows the prompt structure can inject closing delimiters followed by new instructions, effectively adding a new role or instruction block to the prompt. Implement delimiter confusion detection that scans user content for structural tokens before interpolation and either escapes, strips, or blocks the input."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-prompt-injection-detection-via-delimiter-confusion
tags: [prompt-injection, delimiter-confusion, role-injection, structural-token-detection, input-sanitization, llm-security]
symptoms:
  - "User input containing </system> or [/INST] tokens alters agent behavior unexpectedly"
  - "Injected role boundaries in user content override system instructions"
  - "No validation of user-supplied content before it is interpolated into structured prompts"
  - "Attacker can add new tool permissions by injecting a fake system block"
  - "LLM follows instructions from user content that mimics the system prompt format"
---

## Why This Happens

Structured prompt formats use delimiter tokens to separate roles: `<|system|>`, `[INST]`, `<|im_start|>system`, `---`, XML tags, or JSON field boundaries. When user content is interpolated without scanning for these tokens, an attacker who knows the format can close the current block and open a new one. The LLM's tokenizer processes the delimiter literally, treating the injected block as a legitimate role change. Detection requires maintaining a registry of all delimiter patterns used in the active prompt format and scanning every user-supplied string before interpolation. Escaping is preferred over blocking for most cases — the user's intent can usually be preserved by encoding the dangerous characters.

## Solution 1: Delimiter Pattern Registry

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Pattern, Tuple


class DelimiterSeverity(str, Enum):
    CRITICAL = "critical"   # directly injects a role; must block or escape
    HIGH = "high"           # likely structural token; should escape
    MEDIUM = "medium"       # possibly structural; warn and escape
    LOW = "low"             # suspicious but ambiguous; warn only


@dataclass
class DelimiterPattern:
    name: str
    pattern: str            # regex pattern
    severity: DelimiterSeverity
    escape_fn: str = ""     # name of escape strategy to apply
    compiled: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.compiled = re.compile(self.pattern, re.IGNORECASE | re.DOTALL)

    def matches(self, text: str) -> List[Tuple[int, int, str]]:
        return [(m.start(), m.end(), m.group()) for m in self.compiled.finditer(text)]


def build_default_delimiter_registry() -> List[DelimiterPattern]:
    return [
        DelimiterPattern(
            name="chatml_system_open",
            pattern=r"<\|im_start\|>\s*system",
            severity=DelimiterSeverity.CRITICAL,
        ),
        DelimiterPattern(
            name="chatml_any_role",
            pattern=r"<\|im_start\|>\s*\w+",
            severity=DelimiterSeverity.CRITICAL,
        ),
        DelimiterPattern(
            name="chatml_end",
            pattern=r"<\|im_end\|>",
            severity=DelimiterSeverity.CRITICAL,
        ),
        DelimiterPattern(
            name="llama_inst_open",
            pattern=r"\[INST\]",
            severity=DelimiterSeverity.CRITICAL,
        ),
        DelimiterPattern(
            name="llama_inst_close",
            pattern=r"\[/INST\]",
            severity=DelimiterSeverity.CRITICAL,
        ),
        DelimiterPattern(
            name="xml_system_tag",
            pattern=r"</?system\s*/?>",
            severity=DelimiterSeverity.CRITICAL,
        ),
        DelimiterPattern(
            name="xml_user_tag",
            pattern=r"</?(?:user|assistant|human|ai)\s*/?>",
            severity=DelimiterSeverity.HIGH,
        ),
        DelimiterPattern(
            name="anthropic_human",
            pattern=r"\n\s*Human\s*:",
            severity=DelimiterSeverity.HIGH,
        ),
        DelimiterPattern(
            name="anthropic_assistant",
            pattern=r"\n\s*Assistant\s*:",
            severity=DelimiterSeverity.HIGH,
        ),
        DelimiterPattern(
            name="triple_hash_header",
            pattern=r"#{3,}\s*(system|user|assistant|instruction)",
            severity=DelimiterSeverity.MEDIUM,
        ),
        DelimiterPattern(
            name="json_role_field",
            pattern=r'"role"\s*:\s*"system"',
            severity=DelimiterSeverity.HIGH,
        ),
    ]
```

## Solution 2: Delimiter Scanner

```python
from dataclasses import dataclass
from typing import List


@dataclass
class DelimiterScanResult:
    input_text: str
    matches: List[dict]
    max_severity: Optional[DelimiterSeverity]
    is_suspicious: bool

    @classmethod
    def clean(cls, text: str) -> "DelimiterScanResult":
        return cls(input_text=text, matches=[], max_severity=None, is_suspicious=False)


class DelimiterScanner:
    """
    Scans a string for all registered delimiter patterns and returns
    a structured result with match locations and severity levels.
    """

    SEVERITY_RANK = {
        DelimiterSeverity.LOW: 1,
        DelimiterSeverity.MEDIUM: 2,
        DelimiterSeverity.HIGH: 3,
        DelimiterSeverity.CRITICAL: 4,
    }

    def __init__(self, patterns: List[DelimiterPattern]):
        self._patterns = patterns

    def scan(self, text: str) -> DelimiterScanResult:
        all_matches = []
        max_sev = None

        for pattern in self._patterns:
            for start, end, matched in pattern.matches(text):
                all_matches.append({
                    "pattern_name": pattern.name,
                    "severity": pattern.severity.value,
                    "matched_text": matched,
                    "position": start,
                })
                if max_sev is None or self.SEVERITY_RANK[pattern.severity] > self.SEVERITY_RANK[max_sev]:
                    max_sev = pattern.severity

        return DelimiterScanResult(
            input_text=text,
            matches=all_matches,
            max_severity=max_sev,
            is_suspicious=len(all_matches) > 0,
        )
```

## Solution 3: Delimiter Sanitizer

```python
import html
import re
from typing import Callable, Dict


class DelimiterSanitizer:
    """
    Applies escaping or stripping to remove or neutralize delimiter tokens
    from user input. Preserves the semantic content of the input while
    making it structurally inert.
    """

    def __init__(self, patterns: List[DelimiterPattern]):
        self._patterns = patterns

    def escape_angle_brackets(self, text: str) -> str:
        """Replace < and > with HTML entities — neutralizes XML/HTML-style delimiters."""
        return text.replace("<", "&lt;").replace(">", "&gt;")

    def strip_matched_patterns(self, text: str, scan_result: DelimiterScanResult) -> str:
        """Remove all matched delimiter tokens from the text."""
        result = text
        for pattern in self._patterns:
            result = pattern.compiled.sub("", result)
        return result

    def escape_matched_patterns(self, text: str) -> str:
        """
        Replace matched structural tokens with visually similar but
        semantically inert equivalents using Unicode lookalikes.
        """
        result = text
        # Replace < > with fullwidth versions
        result = result.replace("<|", "\uff1c|")
        result = result.replace("|>", "|\uff1e")
        result = result.replace("[INST]", "[\u0399NST]")
        result = result.replace("[/INST]", "[/\u0399NST]")
        # Escape remaining angle brackets
        result = re.sub(r"<(/?)(\w+)(/?)>", r"&lt;\1\2\3&gt;", result)
        return result

    def sanitize(self, text: str, scan_result: DelimiterScanResult) -> str:
        if not scan_result.is_suspicious:
            return text
        if scan_result.max_severity == DelimiterSeverity.CRITICAL:
            return self.strip_matched_patterns(text, scan_result)
        return self.escape_matched_patterns(text)
```

## Solution 4: Injection-Safe Prompt Interpolator

```python
from typing import Any, Dict, Optional


class InjectionSafePromptInterpolator:
    """
    Replaces naive f-string or .format() interpolation with a scanner-
    and-sanitizer pass for every user-supplied value before it is
    inserted into the prompt template.
    """

    def __init__(
        self,
        scanner: DelimiterScanner,
        sanitizer: DelimiterSanitizer,
        block_on_critical: bool = True,
    ):
        self._scanner = scanner
        self._sanitizer = sanitizer
        self._block = block_on_critical
        self._blocked_count = 0
        self._sanitized_count = 0

    def interpolate(self, template: str, variables: Dict[str, Any]) -> str:
        safe_vars = {}
        for key, value in variables.items():
            if not isinstance(value, str):
                safe_vars[key] = value
                continue
            scan = self._scanner.scan(value)
            if scan.is_suspicious:
                if self._block and scan.max_severity == DelimiterSeverity.CRITICAL:
                    self._blocked_count += 1
                    raise DelimiterInjectionBlocked(
                        field=key,
                        matched=scan.matches,
                    )
                safe_vars[key] = self._sanitizer.sanitize(value, scan)
                self._sanitized_count += 1
            else:
                safe_vars[key] = value
        return template.format(**safe_vars)

    def stats(self) -> dict:
        return {
            "blocked_injections": self._blocked_count,
            "sanitized_inputs": self._sanitized_count,
        }


class DelimiterInjectionBlocked(Exception):
    def __init__(self, field: str, matched: list):
        super().__init__(f"delimiter injection blocked in field '{field}': {[m['pattern_name'] for m in matched]}")
        self.field = field
        self.matched = matched
```

## Solution 5: Injection Attempt Auditor

```python
import time
from typing import List


class InjectionAttemptAuditor:
    """
    Records detected delimiter injection attempts for security analysis.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, field: str, scan_result: DelimiterScanResult, session_id: str = "") -> None:
        if not scan_result.is_suspicious:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "field": field,
            "max_severity": scan_result.max_severity.value if scan_result.max_severity else None,
            "pattern_names": [m["pattern_name"] for m in scan_result.matches],
            "input_preview": scan_result.input_text[:100],
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        from collections import Counter
        pattern_freq = Counter(
            p for r in recent for p in r["pattern_names"]
        )
        return {
            "window_seconds": window_seconds,
            "attempts": len(recent),
            "unique_sessions": len({r["session_id"] for r in recent}),
            "top_patterns": pattern_freq.most_common(5),
        }
```

## Solution 6: Delimiter Injection Security Dashboard

```python
import time


class DelimiterInjectionDashboard:
    """
    Combines scanner configuration, interpolator stats, and audit summary.
    """

    def __init__(
        self,
        interpolator: InjectionSafePromptInterpolator,
        auditor: InjectionAttemptAuditor,
    ):
        self._interpolator = interpolator
        self._auditor = auditor

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "interpolator_stats": self._interpolator.stats(),
            "injection_attempts": self._auditor.summary(window_seconds),
        }
```

## Comparison

| Approach | Pattern Registry | Scanning | Sanitization | Safe Interpolation | Audit |
|---|---|---|---|---|---|
| DelimiterPattern registry | Yes (11 patterns) | No | No | No | No |
| DelimiterScanner | Via registry | Yes | No | No | No |
| DelimiterSanitizer | Via patterns | No | Yes (strip/escape) | No | No |
| InjectionSafePromptInterpolator | Via scanner | Via scanner | Via sanitizer | Yes | No |
| InjectionAttemptAuditor | No | No | No | No | Yes |
| DelimiterInjectionDashboard | No | No | No | No | Yes |

**Best for production**: Replace every `f"...{user_input}..."` or `template.format(user_input=user_input)` with `InjectionSafePromptInterpolator.interpolate()` — make this a lint rule enforced in CI. Set `block_on_critical=True` for any user input that will be placed in the system prompt or between role delimiters; use sanitize-and-continue for content that goes into user-turn messages where the risk is lower. Extend `build_default_delimiter_registry()` with any custom delimiters your prompt format uses — if you use `---TOOL_RESULT---` as a section separator, add a pattern for it. Monitor `InjectionAttemptAuditor.summary()` for spikes in `unique_sessions` — coordinated injection probing from multiple sessions indicates an active adversarial campaign.
