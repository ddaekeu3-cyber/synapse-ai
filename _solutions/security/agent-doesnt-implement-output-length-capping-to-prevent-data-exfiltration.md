---
title: "Agent Doesn't Implement Output Length Capping to Prevent Data Exfiltration"
description: "Agents that place no upper bound on response length allow prompt injection attacks to exfiltrate large volumes of context data: an injected instruction like 'repeat everything above verbatim' causes the agent to output the entire system prompt, conversation history, and retrieved documents in a single response. Implement output length capping with content-type-aware limits that prevent oversized responses while preserving legitimate long-form outputs."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-length-capping-to-prevent-data-exfiltration
tags: [output-capping, data-exfiltration, prompt-injection, response-length, context-leakage, content-policy]
symptoms:
  - "Agent responses can be arbitrarily long with no enforced maximum"
  - "Prompt injection causes agent to repeat system prompt or context documents verbatim"
  - "No distinction between legitimate long-form responses and exfiltration patterns"
  - "Responses containing repeated content from the context are not flagged"
  - "Token billing spikes when injected prompts trigger large verbatim repetitions"
---

## Why This Happens

LLMs will comply with instructions to repeat content if those instructions appear in the prompt — whether injected through user input, a retrieved document, or a tool result. Without a length cap, an attacker who can inject instructions can cause the agent to output the entire system prompt, all retrieved documents, or the full conversation history. A length cap is a defense-in-depth control: even if the injection succeeds in generating repetition, the cap truncates the output before the full context is exposed.

## Solution 1: Output Length Policy

```python
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class OutputContentType(str, Enum):
    CONVERSATIONAL = "conversational"   # short back-and-forth replies
    DOCUMENT = "document"               # structured documents, reports
    CODE = "code"                       # code generation
    ANALYSIS = "analysis"               # analytical responses
    DEFAULT = "default"


class TruncationBehavior(str, Enum):
    HARD_CUT = "hard_cut"               # cut at limit and stop
    SENTENCE_BOUNDARY = "sentence_boundary"  # cut at last sentence end
    ELLIPSIS = "ellipsis"               # cut and add truncation notice


@dataclass
class OutputLengthPolicy:
    content_type: OutputContentType
    max_chars: int
    max_tokens_estimate: int
    truncation: TruncationBehavior = TruncationBehavior.SENTENCE_BOUNDARY
    warn_at_pct: float = 0.80           # warn when output reaches 80% of limit
    repetition_detection: bool = True   # check for verbatim repetition patterns


DEFAULT_POLICIES: Dict[OutputContentType, OutputLengthPolicy] = {
    OutputContentType.CONVERSATIONAL: OutputLengthPolicy(
        content_type=OutputContentType.CONVERSATIONAL,
        max_chars=4000,
        max_tokens_estimate=1000,
    ),
    OutputContentType.DOCUMENT: OutputLengthPolicy(
        content_type=OutputContentType.DOCUMENT,
        max_chars=20000,
        max_tokens_estimate=5000,
    ),
    OutputContentType.CODE: OutputLengthPolicy(
        content_type=OutputContentType.CODE,
        max_chars=32000,
        max_tokens_estimate=8000,
    ),
    OutputContentType.ANALYSIS: OutputLengthPolicy(
        content_type=OutputContentType.ANALYSIS,
        max_chars=12000,
        max_tokens_estimate=3000,
    ),
    OutputContentType.DEFAULT: OutputLengthPolicy(
        content_type=OutputContentType.DEFAULT,
        max_chars=8000,
        max_tokens_estimate=2000,
    ),
}
```

## Solution 2: Repetition Pattern Detector

```python
import re
from typing import List, Optional, Tuple


class RepetitionPatternDetector:
    """
    Detects verbatim repetition of known context strings in agent output.
    Used to catch exfiltration attempts that repeat system prompt or documents.
    """

    def __init__(
        self,
        min_repeated_length: int = 50,    # minimum chars to flag as repetition
        similarity_threshold: float = 0.90,
    ):
        self._min_len = min_repeated_length
        self._threshold = similarity_threshold

    def _normalize(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    def check(
        self,
        output: str,
        context_sources: List[str],    # system prompt, retrieved docs, etc.
    ) -> Tuple[bool, List[str]]:
        """
        Returns (has_repetition, list of matched fragments).
        """
        norm_output = self._normalize(output)
        matches = []

        for source in context_sources:
            norm_source = self._normalize(source)
            if len(norm_source) < self._min_len:
                continue
            # Check for substantial overlap using sliding window
            window = self._min_len
            for start in range(0, len(norm_source) - window, window // 2):
                fragment = norm_source[start:start + window]
                if fragment in norm_output:
                    matches.append(source[:80] + "...")
                    break

        return len(matches) > 0, matches
```

## Solution 3: Output Truncator

```python
import re
from typing import Tuple


class OutputTruncator:
    """
    Truncates output to comply with length policy.
    Supports hard cut, sentence-boundary truncation, and ellipsis.
    """

    TRUNCATION_NOTICE = "\n\n[Response truncated for length.]"

    def truncate(self, text: str, policy: OutputLengthPolicy) -> Tuple[str, bool]:
        """Returns (truncated_text, was_truncated)."""
        if len(text) <= policy.max_chars:
            return text, False

        limit = policy.max_chars - len(self.TRUNCATION_NOTICE)

        if policy.truncation == TruncationBehavior.HARD_CUT:
            return text[:limit], True

        if policy.truncation == TruncationBehavior.SENTENCE_BOUNDARY:
            truncated = text[:limit]
            # Find last sentence boundary
            last_period = max(
                truncated.rfind(". "),
                truncated.rfind(".\n"),
                truncated.rfind("! "),
                truncated.rfind("? "),
            )
            if last_period > limit * 0.6:   # don't cut too aggressively
                truncated = truncated[:last_period + 1]
            return truncated + self.TRUNCATION_NOTICE, True

        if policy.truncation == TruncationBehavior.ELLIPSIS:
            return text[:limit] + self.TRUNCATION_NOTICE, True

        return text[:limit], True
```

## Solution 4: Output Length Enforcer

```python
import time
from typing import List, Optional


class OutputLengthEnforcer:
    """
    Applies length policy and repetition detection to agent output.
    Records all enforcement actions for security auditing.
    """

    def __init__(
        self,
        policies: dict,
        truncator: OutputTruncator,
        repetition_detector: RepetitionPatternDetector,
    ):
        self._policies = policies
        self._truncator = truncator
        self._detector = repetition_detector
        self._enforcement_log = []
        self._truncation_count = 0
        self._repetition_flags = 0

    def enforce(
        self,
        output: str,
        content_type: OutputContentType = OutputContentType.DEFAULT,
        context_sources: Optional[List[str]] = None,
        session_id: str = "",
    ) -> dict:
        policy = self._policies.get(content_type, self._policies[OutputContentType.DEFAULT])
        original_len = len(output)
        warnings = []

        # Repetition check before truncation
        repetition_found = False
        if policy.repetition_detection and context_sources:
            repetition_found, matches = self._detector.check(output, context_sources)
            if repetition_found:
                self._repetition_flags += 1
                warnings.append(f"repetition_detected: {len(matches)} context source(s) repeated")

        # Length check
        was_truncated = False
        if original_len > policy.warn_at_pct * policy.max_chars:
            warnings.append(f"approaching_limit: {original_len}/{policy.max_chars} chars")

        output, was_truncated = self._truncator.truncate(output, policy)

        if was_truncated:
            self._truncation_count += 1

        if was_truncated or repetition_found:
            self._enforcement_log.append({
                "ts": time.time(),
                "session_id": session_id,
                "original_len": original_len,
                "final_len": len(output),
                "truncated": was_truncated,
                "repetition_found": repetition_found,
                "content_type": content_type.value,
            })
            if len(self._enforcement_log) > 5000:
                self._enforcement_log.pop(0)

        return {
            "output": output,
            "original_length": original_len,
            "final_length": len(output),
            "truncated": was_truncated,
            "repetition_detected": repetition_found,
            "warnings": warnings,
            "safe": not repetition_found,
        }

    def stats(self) -> dict:
        return {
            "truncation_count": self._truncation_count,
            "repetition_flags": self._repetition_flags,
        }
```

## Solution 5: Streaming Output Length Guard

```python
import asyncio
from typing import AsyncGenerator, List, Optional


class StreamingOutputLengthGuard:
    """
    Applies length limits to streaming output by counting emitted chars
    and cutting the stream when the limit is reached.
    """

    def __init__(self, policy: OutputLengthPolicy, truncator: OutputTruncator):
        self._policy = policy
        self._truncator = truncator

    async def guard(
        self,
        stream: AsyncGenerator[str, None],
    ) -> AsyncGenerator[str, None]:
        total_chars = 0
        async for chunk in stream:
            remaining = self._policy.max_chars - total_chars
            if remaining <= 0:
                yield OutputTruncator.TRUNCATION_NOTICE
                return
            if len(chunk) > remaining:
                yield chunk[:remaining]
                yield OutputTruncator.TRUNCATION_NOTICE
                return
            yield chunk
            total_chars += len(chunk)
```

## Solution 6: Output Length Security Dashboard

```python
import time


class OutputLengthSecurityDashboard:
    """
    Combines enforcement stats and recent flagged outputs into
    a security monitoring view.
    """

    def __init__(self, enforcer: OutputLengthEnforcer):
        self._enforcer = enforcer

    def render(self) -> dict:
        recent_flags = [
            e for e in self._enforcer._enforcement_log
            if time.time() - e["ts"] < 3600
        ]
        stats = self._enforcer.stats()

        return {
            "generated_at": time.time(),
            "stats": stats,
            "recent_flags_1h": len(recent_flags),
            "repetition_events_1h": sum(1 for e in recent_flags if e["repetition_found"]),
            "truncation_events_1h": sum(1 for e in recent_flags if e["truncated"]),
            "alert": sum(1 for e in recent_flags if e["repetition_found"]) > 0,
        }
```

## Comparison

| Approach | Length Enforcement | Repetition Detection | Streaming Support | Policy Per-Type | Security Audit |
|---|---|---|---|---|---|
| OutputLengthPolicy | Yes (config) | No | No | Yes | No |
| RepetitionPatternDetector | No | Yes (fragment match) | No | No | No |
| OutputTruncator | Yes (3 modes) | No | No | No | No |
| OutputLengthEnforcer | Via truncator | Via detector | No | Via policies | Yes |
| StreamingOutputLengthGuard | Yes (streaming) | No | Yes | No | No |
| OutputLengthSecurityDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_chars=4000` for conversational responses — legitimate answers to user questions rarely need more than 1000 tokens. Use `RepetitionPatternDetector` with the system prompt and any retrieved documents as `context_sources` — these are the most valuable exfiltration targets. Alert immediately on `repetition_detected=True` in the enforcement log: this is a strong signal of an active prompt injection attack, not a false positive. Apply `StreamingOutputLengthGuard` to streaming endpoints — truncating after the full response is generated does not prevent the client from receiving the exfiltrated content mid-stream.
