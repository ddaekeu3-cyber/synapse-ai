---
title: "Agent Doesn't Implement Prompt Compression for Long Conversation History"
description: "Agents that include the full raw conversation history in every prompt pay linearly growing token costs as conversations lengthen — a 50-turn conversation may consume 80% of the context window before any tool results or system instructions are added. Implement prompt compression that summarizes older turns, removes redundant content, and preserves only the information needed for coherent continuation."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-compression-for-long-conversation-history
tags: [prompt-compression, conversation-history, context-management, token-efficiency, summarization, context-window]
symptoms:
  - "Token usage grows linearly with conversation length — 50 turns cost 50× as much as 1 turn"
  - "Context window fills with old conversation turns, leaving no room for tool results"
  - "Model starts forgetting early instructions once conversation history dominates context"
  - "No truncation or summarization of old messages — everything is always included"
  - "Cost per request correlates directly with session age rather than query complexity"
---

## Why This Happens

Raw conversation history is the most token-expensive component of long-running agent sessions. Every message — user and assistant — accumulates without bound. A 50-turn conversation with 200 tokens per turn consumes 10,000 tokens before any system prompt or tool result is added. Prompt compression addresses this by applying a tiered strategy: recent turns are kept verbatim (high information value), middle-aged turns are compressed to key facts, and old turns are summarized into a single paragraph or discarded if fully superseded. The goal is to preserve continuity while shrinking token count.

## Solution 1: Conversation Turn Classifier

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TurnRetentionTier(str, Enum):
    VERBATIM = "verbatim"       # keep as-is (recent turns)
    COMPRESSED = "compressed"   # extract key facts only
    SUMMARIZED = "summarized"   # merge into rolling summary
    DISCARDED = "discarded"     # remove entirely


@dataclass
class ClassifiedTurn:
    role: str
    content: str
    turn_index: int
    token_estimate: int
    tier: TurnRetentionTier
    key_facts: Optional[str] = None     # populated for COMPRESSED tier
    recorded_at: float = field(default_factory=time.time)


class ConversationTurnClassifier:
    """
    Assigns retention tiers to conversation turns based on recency.
    Recent N turns: VERBATIM
    Next M turns: COMPRESSED
    Remainder: SUMMARIZED or DISCARDED
    """

    def __init__(
        self,
        verbatim_turns: int = 6,
        compressed_turns: int = 10,
        chars_per_token: float = 4.0,
    ):
        self._verbatim = verbatim_turns
        self._compressed = compressed_turns
        self._chars_per_token = chars_per_token

    def classify(self, turns: list) -> List[ClassifiedTurn]:
        """turns: list of {role, content} dicts, ordered oldest-first."""
        total = len(turns)
        result = []
        for i, turn in enumerate(turns):
            distance_from_end = total - 1 - i
            content = turn.get("content", "")
            token_est = max(1, int(len(content) / self._chars_per_token))

            if distance_from_end < self._verbatim:
                tier = TurnRetentionTier.VERBATIM
            elif distance_from_end < self._verbatim + self._compressed:
                tier = TurnRetentionTier.COMPRESSED
            else:
                tier = TurnRetentionTier.SUMMARIZED

            result.append(ClassifiedTurn(
                role=turn.get("role", "user"),
                content=content,
                turn_index=i,
                token_estimate=token_est,
                tier=tier,
            ))
        return result
```

## Solution 2: Key Fact Extractor

```python
import re
from typing import List


class KeyFactExtractor:
    """
    Extracts key facts from a conversation turn for the COMPRESSED tier.
    Uses heuristics: sentences containing nouns + verbs, explicit decisions,
    named entities, and numeric values.
    """

    DECISION_PATTERNS = re.compile(
        r"(decided|confirmed|agreed|set|changed|selected|chose|rejected|approved)\s+.{0,100}",
        re.IGNORECASE,
    )
    NUMERIC_FACT = re.compile(r"\b\d[\d,.]*\s*(?:%|dollars?|USD|ms|seconds?|hours?|GB|MB|items?)\b")

    def extract(self, content: str, max_facts: int = 3) -> str:
        sentences = [s.strip() for s in re.split(r"[.!?]\s+", content) if len(s.strip()) > 20]
        facts = []

        # Prioritize decision sentences
        for s in sentences:
            if self.DECISION_PATTERNS.search(s):
                facts.append(s)
                if len(facts) >= max_facts:
                    break

        # Add numeric fact sentences
        if len(facts) < max_facts:
            for s in sentences:
                if s not in facts and self.NUMERIC_FACT.search(s):
                    facts.append(s)
                    if len(facts) >= max_facts:
                        break

        # Fill with first sentences if still short
        if len(facts) < max_facts:
            for s in sentences:
                if s not in facts:
                    facts.append(s)
                    if len(facts) >= max_facts:
                        break

        return ". ".join(facts[:max_facts]) + ("." if facts else "")
```

## Solution 3: Conversation Compressor

```python
from typing import List, Optional


class ConversationCompressor:
    """
    Produces a compressed version of the conversation history:
    - VERBATIM turns are included as-is
    - COMPRESSED turns are reduced to key facts
    - SUMMARIZED turns are merged into a single prefix paragraph
    """

    def __init__(
        self,
        classifier: ConversationTurnClassifier,
        extractor: KeyFactExtractor,
    ):
        self._classifier = classifier
        self._extractor = extractor

    def compress(self, turns: list) -> dict:
        classified = self._classifier.classify(turns)
        original_tokens = sum(t.token_estimate for t in classified)

        summarized_turns = [t for t in classified if t.tier == TurnRetentionTier.SUMMARIZED]
        compressed_turns = [t for t in classified if t.tier == TurnRetentionTier.COMPRESSED]
        verbatim_turns = [t for t in classified if t.tier == TurnRetentionTier.VERBATIM]

        # Build rolling summary from old turns
        summary_text = ""
        if summarized_turns:
            facts = []
            for t in summarized_turns:
                fact = self._extractor.extract(t.content, max_facts=1)
                if fact:
                    facts.append(f"[{t.role}]: {fact}")
            if facts:
                summary_text = "Earlier conversation summary: " + " | ".join(facts)

        # Build output turns
        output_turns = []
        if summary_text:
            output_turns.append({"role": "system", "content": summary_text})

        for t in compressed_turns:
            key_facts = self._extractor.extract(t.content, max_facts=2)
            output_turns.append({"role": t.role, "content": key_facts or t.content[:100]})

        for t in verbatim_turns:
            output_turns.append({"role": t.role, "content": t.content})

        compressed_tokens = sum(
            max(1, int(len(m["content"]) / 4)) for m in output_turns
        )

        return {
            "turns": output_turns,
            "original_turn_count": len(turns),
            "output_turn_count": len(output_turns),
            "original_tokens_est": original_tokens,
            "compressed_tokens_est": compressed_tokens,
            "tokens_saved_est": max(0, original_tokens - compressed_tokens),
            "compression_ratio": round(compressed_tokens / max(original_tokens, 1), 4),
        }
```

## Solution 4: Progressive Compression Trigger

```python
from typing import List


class ProgressiveCompressionTrigger:
    """
    Decides when to apply compression based on token budget utilization.
    Applies light compression at 60% context usage, heavy compression at 80%.
    """

    def __init__(
        self,
        context_window_tokens: int = 128000,
        light_threshold: float = 0.60,
        heavy_threshold: float = 0.80,
        chars_per_token: float = 4.0,
    ):
        self._window = context_window_tokens
        self._light = light_threshold
        self._heavy = heavy_threshold
        self._cpt = chars_per_token

    def evaluate(
        self,
        system_prompt: str,
        history: List[dict],
        tool_results_chars: int = 0,
    ) -> dict:
        sys_tokens = int(len(system_prompt) / self._cpt)
        history_tokens = sum(int(len(t.get("content", "")) / self._cpt) for t in history)
        tool_tokens = int(tool_results_chars / self._cpt)
        total = sys_tokens + history_tokens + tool_tokens
        utilization = total / max(self._window, 1)

        if utilization >= self._heavy:
            action = "heavy"
            verbatim = 4
            compressed = 6
        elif utilization >= self._light:
            action = "light"
            verbatim = 8
            compressed = 12
        else:
            action = "none"
            verbatim = 999
            compressed = 999

        return {
            "action": action,
            "utilization": round(utilization, 4),
            "total_tokens_est": total,
            "history_tokens_est": history_tokens,
            "recommended_verbatim_turns": verbatim,
            "recommended_compressed_turns": compressed,
        }
```

## Solution 5: Compression Savings Tracker

```python
import time
from typing import List


class CompressionSavingsTracker:
    """
    Accumulates compression results across sessions to quantify
    token savings and track compression ratio trends.
    """

    def __init__(self):
        self._records: List[dict] = []

    def record(self, compression_result: dict) -> None:
        self._records.append({
            "ts": time.time(),
            "tokens_saved": compression_result.get("tokens_saved_est", 0),
            "original_tokens": compression_result.get("original_tokens_est", 0),
            "compression_ratio": compression_result.get("compression_ratio", 1.0),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "compressions": 0}
        total_saved = sum(r["tokens_saved"] for r in recent)
        total_original = sum(r["original_tokens"] for r in recent)
        avg_ratio = sum(r["compression_ratio"] for r in recent) / len(recent)
        return {
            "window_seconds": window_seconds,
            "compressions": len(recent),
            "total_tokens_saved_est": total_saved,
            "savings_pct": round(total_saved / max(total_original, 1) * 100, 1),
            "avg_compression_ratio": round(avg_ratio, 4),
        }
```

## Solution 6: Prompt Compression Dashboard

```python
import time


class PromptCompressionDashboard:
    """
    Combines trigger evaluation stats, compression savings, and
    classifier tier distribution into an operational compression report.
    """

    def __init__(
        self,
        compressor: ConversationCompressor,
        tracker: CompressionSavingsTracker,
        trigger: ProgressiveCompressionTrigger,
    ):
        self._compressor = compressor
        self._tracker = tracker
        self._trigger = trigger

    def render(self, sample_history: list = None) -> dict:
        result = {
            "generated_at": time.time(),
            "savings_1h": self._tracker.summary(window_seconds=3600.0),
            "context_window_tokens": self._trigger._window,
        }
        if sample_history:
            evaluation = self._trigger.evaluate("", sample_history)
            result["current_utilization"] = evaluation
        return result
```

## Comparison

| Approach | Recency Tiering | Key Fact Extraction | Rolling Summary | Adaptive Triggering | Savings Tracking |
|---|---|---|---|---|---|
| ConversationTurnClassifier | Yes (3 tiers) | No | No | No | No |
| KeyFactExtractor | No | Yes (heuristic) | No | No | No |
| ConversationCompressor | Via classifier | Via extractor | Yes | No | No |
| ProgressiveCompressionTrigger | No | No | No | Yes (light/heavy) | No |
| CompressionSavingsTracker | No | No | No | No | Yes |
| PromptCompressionDashboard | No | No | No | No | Yes |

**Best for production**: Trigger `heavy` compression at 80% context utilization — waiting until 95% leaves too little room for the next tool result. Keep the last 4–6 turns verbatim unconditionally; these are the most contextually relevant for coherent continuation. Use the rolling summary prefix (system role) rather than discarding old turns silently — the model benefits from knowing what was discussed even in condensed form. Monitor `savings_pct` via the tracker: consistently above 40% means sessions are regularly long enough that compression is the norm, not the exception, and you should consider whether session length should be bounded.
