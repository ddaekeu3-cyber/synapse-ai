---
title: "Agent Doesn't Implement Tool Call Deduplication Within a Turn"
description: "Agents that allow the LLM to generate duplicate tool calls within a single turn execute the same external operation multiple times: two identical web searches consuming double quota, two write operations creating duplicate records, two payment API calls charging twice. Implement within-turn tool call deduplication that detects identical or near-identical calls before dispatch and returns the cached result from the first execution."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-call-deduplication-within-a-turn
tags: [deduplication, tool-calls, within-turn, idempotency, duplicate-detection, quota-protection]
symptoms:
  - "LLM generates duplicate tool calls in the same turn and both are executed"
  - "Search API quota consumed twice for identical queries within one response"
  - "Write operations executed twice creating duplicate database records"
  - "No check for whether a tool with the same arguments was already called this turn"
  - "Parallel tool call batches contain identical entries that are dispatched independently"
---

## Why This Happens

LLMs occasionally generate duplicate tool calls within a single response — two identical search queries, two calls to the same lookup with the same arguments, or two writes to the same resource. This is especially common when the model is generating a list of tool calls in parallel: if the model is uncertain whether it already included a call, it may add it twice. Without deduplication, the agent dispatches all generated calls regardless of overlap, wasting quota, causing side effects, or corrupting state. Within-turn deduplication requires fingerprinting each call before dispatch and short-circuiting any call whose fingerprint was already seen in this turn.

## Solution 1: Tool Call Fingerprint

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class ToolCallFingerprint:
    tool_name: str
    args_hash: str   # SHA-256 of canonicalized arguments

    @staticmethod
    def from_call(tool_name: str, args: Dict[str, Any]) -> "ToolCallFingerprint":
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
        args_hash = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        return ToolCallFingerprint(tool_name=tool_name, args_hash=args_hash)

    def key(self) -> str:
        return f"{self.tool_name}::{self.args_hash}"
```

## Solution 2: Turn-Scoped Deduplication State

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DeduplicatedCallRecord:
    fingerprint: ToolCallFingerprint
    first_seen_at: float
    result: Any
    execution_count: int = 1
    deduplicated_count: int = 0


class TurnScopedDeduplicationState:
    """
    Tracks tool call fingerprints and results within a single agent turn.
    Must be reset between turns.
    """

    def __init__(self):
        self._seen: Dict[str, DeduplicatedCallRecord] = {}
        self._turn_id: Optional[str] = None

    def begin_turn(self, turn_id: str) -> None:
        self._seen = {}
        self._turn_id = turn_id

    def check(
        self, tool_name: str, args: Dict[str, Any]
    ) -> tuple:
        """
        Returns (is_duplicate, cached_result_or_None).
        """
        fp = ToolCallFingerprint.from_call(tool_name, args)
        record = self._seen.get(fp.key())
        if record is not None:
            record.deduplicated_count += 1
            return True, record.result
        return False, None

    def record_result(
        self, tool_name: str, args: Dict[str, Any], result: Any
    ) -> None:
        fp = ToolCallFingerprint.from_call(tool_name, args)
        self._seen[fp.key()] = DeduplicatedCallRecord(
            fingerprint=fp,
            first_seen_at=time.time(),
            result=result,
        )

    def dedup_summary(self) -> dict:
        total_calls = sum(r.execution_count + r.deduplicated_count for r in self._seen.values())
        total_deduped = sum(r.deduplicated_count for r in self._seen.values())
        return {
            "turn_id": self._turn_id,
            "unique_calls": len(self._seen),
            "total_calls": total_calls,
            "deduplicated": total_deduped,
            "dedup_rate": round(total_deduped / max(total_calls, 1), 4),
        }
```

## Solution 3: Deduplicating Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class DeduplicatingToolDispatcher:
    """
    Wraps tool dispatch with within-turn deduplication.
    Identical calls in the same turn return the cached result immediately.
    Non-idempotent tools can be exempted from deduplication.
    """

    def __init__(
        self,
        dedup_state: TurnScopedDeduplicationState,
        non_idempotent_tools: Optional[set] = None,
    ):
        self._state = dedup_state
        self._non_idempotent = non_idempotent_tools or set()
        self._events: list = []

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> dict:
        # Non-idempotent tools always execute
        if tool_name in self._non_idempotent:
            result = await tool_fn(**args)
            return {"result": result, "deduplicated": False, "non_idempotent": True}

        is_dup, cached = self._state.check(tool_name, args)
        if is_dup:
            self._events.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "action": "deduplicated",
            })
            return {"result": cached, "deduplicated": True, "non_idempotent": False}

        result = await tool_fn(**args)
        self._state.record_result(tool_name, args, result)
        return {"result": result, "deduplicated": False, "non_idempotent": False}

    def dedup_events(self) -> list:
        return list(self._events)
```

## Solution 4: Near-Duplicate Call Detector

```python
import re
from typing import Any, Dict, List, Optional, Tuple


class NearDuplicateToolCallDetector:
    """
    Detects tool calls that are semantically equivalent but not byte-identical:
    the same search query with different casing, extra whitespace, or minor
    phrasing differences. Uses normalized string similarity on string arguments.
    """

    def __init__(self, similarity_threshold: float = 0.90):
        self._threshold = similarity_threshold
        self._seen_calls: List[Tuple[str, str]] = []  # (tool_name, normalized_args)

    def _normalize(self, args: Dict[str, Any]) -> str:
        flat = " ".join(
            str(v).lower().strip()
            for v in self._flatten_values(args)
            if isinstance(v, (str, int, float))
        )
        return re.sub(r"\s+", " ", flat).strip()

    @staticmethod
    def _flatten_values(obj: Any) -> List[Any]:
        if isinstance(obj, dict):
            return [v for val in obj.values() for v in NearDuplicateToolCallDetector._flatten_values(val)]
        if isinstance(obj, list):
            return [v for item in obj for v in NearDuplicateToolCallDetector._flatten_values(item)]
        return [obj]

    def _jaccard(self, a: str, b: str) -> float:
        tokens_a = set(a.split())
        tokens_b = set(b.split())
        if not tokens_a and not tokens_b:
            return 1.0
        intersection = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        return intersection / union if union else 0.0

    def check(self, tool_name: str, args: Dict[str, Any]) -> Tuple[bool, float]:
        """Returns (is_near_duplicate, max_similarity)."""
        normalized = self._normalize(args)
        max_sim = 0.0
        for seen_tool, seen_norm in self._seen_calls:
            if seen_tool != tool_name:
                continue
            sim = self._jaccard(normalized, seen_norm)
            if sim > max_sim:
                max_sim = sim
        self._seen_calls.append((tool_name, normalized))
        return max_sim >= self._threshold, round(max_sim, 4)

    def reset(self) -> None:
        self._seen_calls = []
```

## Solution 5: Dedup Audit Logger

```python
import time
from typing import List


class ToolCallDedupAuditLogger:
    """
    Records deduplication events across turns for analysis.
    Helps identify which tools and argument patterns produce most duplicates.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def log(
        self,
        tool_name: str,
        dedup_type: str,   # "exact" | "near_duplicate"
        similarity: Optional[float],
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "dedup_type": dedup_type,
            "similarity": similarity,
            "session_id": session_id,
            "turn_id": turn_id,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "events": 0}

        from collections import Counter
        tool_counts = Counter(r["tool_name"] for r in recent)
        return {
            "window_seconds": window_seconds,
            "dedup_events": len(recent),
            "exact_duplicates": sum(1 for r in recent if r["dedup_type"] == "exact"),
            "near_duplicates": sum(1 for r in recent if r["dedup_type"] == "near_duplicate"),
            "top_deduplicated_tools": tool_counts.most_common(5),
        }
```

## Solution 6: Turn Dedup Dashboard

```python
import time


class TurnDeduplicationDashboard:
    """
    Combines turn-level dedup state, near-duplicate detection,
    and audit history into a single operational view.
    """

    def __init__(
        self,
        dedup_state: TurnScopedDeduplicationState,
        near_dup_detector: NearDuplicateToolCallDetector,
        audit_logger: ToolCallDedupAuditLogger,
    ):
        self._state = dedup_state
        self._near = near_dup_detector
        self._logger = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "current_turn": self._state.dedup_summary(),
            "audit_last_hour": self._audit_logger.summary(window_seconds=3600.0),
        }

    @property
    def _audit_logger(self):
        return self._logger
```

## Comparison

| Approach | Exact Dedup | Near-Duplicate | Non-Idempotent Bypass | Audit Trail | Turn Reset |
|---|---|---|---|---|---|
| TurnScopedDeduplicationState | Yes (SHA-256) | No | No | No | Yes |
| DeduplicatingToolDispatcher | Via state | No | Yes (exemption set) | No | Via state |
| NearDuplicateToolCallDetector | No | Yes (Jaccard) | No | No | Yes (reset) |
| ToolCallDedupAuditLogger | No | No | No | Yes | No |

**Best for production**: Add write operations, payment tools, and any tool with irreversible side effects to `non_idempotent_tools` — these must never be deduplicated even when arguments match exactly. For read-only tools (search, lookup, fetch), exact deduplication is always safe and should be the default. Set `NearDuplicateToolCallDetector` similarity threshold to 0.90 to catch trivially rephrased queries (extra article, different capitalization) without false-positiving on genuinely different queries. Monitor `top_deduplicated_tools` in `ToolCallDedupAuditLogger`: a tool with high dedup rates is being over-requested by the model and may benefit from a prompt instruction to avoid calling it more than once per turn.
