---
title: "Agent Doesn't Implement Tool Call Deduplication Within a Single Turn"
description: "Agents that allow the LLM to emit duplicate tool calls within a single response execute the same operation multiple times: the same database row is queried twice, the same API endpoint is hit twice, or the same file is read twice — wasting tokens, latency, and downstream quota. Implement within-turn tool call deduplication that detects semantically identical calls before execution and returns the cached result for the duplicate."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-call-deduplication-within-a-single-turn
tags: [tool-deduplication, within-turn, duplicate-calls, llm-loop, call-fingerprint, execution-efficiency]
symptoms:
  - "LLM emits two identical tool calls in a single response and both are executed"
  - "Same read tool called with identical arguments twice in one turn"
  - "Duplicate API calls inflate quota usage and downstream rate limits"
  - "No fingerprinting of tool calls before dispatch to detect repeats"
  - "Tool result list contains identical entries from duplicate execution"
---

## Why This Happens

LLMs can emit the same tool call more than once in a single response, especially when the context is long or the model is uncertain. Without a deduplication layer, the agent dispatches every call in the list. For read-only tools this wastes time and quota; for write tools it can cause duplicate side effects. Within-turn deduplication requires fingerprinting each call by tool name and arguments before execution, keeping a set of already-dispatched fingerprints for the current turn, and returning the previously computed result for any call whose fingerprint is already in the set.

## Solution 1: Turn-Scoped Call Fingerprinter

```python
import hashlib
import json
from typing import Any, Dict


class TurnScopedCallFingerprinter:
    """
    Produces a stable fingerprint for a (tool_name, arguments) pair.
    Two calls with identical tool names and argument values produce
    the same fingerprint regardless of JSON key ordering.
    """

    def fingerprint(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        payload = {"tool": tool_name, "args": arguments}
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:20]

    def fingerprint_list(
        self,
        calls: list,
    ) -> list:
        """
        Returns a list of (call, fingerprint) tuples.
        calls: list of dicts with 'name'/'tool_name' and 'arguments' keys.
        """
        result = []
        for call in calls:
            name = call.get("name") or call.get("tool_name", "")
            args = call.get("arguments") or call.get("input") or {}
            result.append((call, self.fingerprint(name, args)))
        return result
```

## Solution 2: Within-Turn Deduplication Registry

```python
from typing import Any, Dict, Optional, Set


class WithinTurnDeduplicationRegistry:
    """
    Maintains the set of call fingerprints dispatched in the current turn.
    Stores results keyed by fingerprint so duplicates receive the
    same result without re-execution.
    """

    def __init__(self):
        self._seen: Set[str] = set()
        self._results: Dict[str, Any] = {}
        self._dedup_count = 0

    def is_duplicate(self, fingerprint: str) -> bool:
        return fingerprint in self._seen

    def register(self, fingerprint: str, result: Any) -> None:
        self._seen.add(fingerprint)
        self._results[fingerprint] = result

    def get_result(self, fingerprint: str) -> Optional[Any]:
        return self._results.get(fingerprint)

    def record_dedup(self) -> None:
        self._dedup_count += 1

    def reset(self) -> None:
        """Call at the start of each new turn."""
        self._seen.clear()
        self._results.clear()

    def stats(self) -> dict:
        return {
            "dispatched_this_turn": len(self._seen),
            "dedup_hits_this_turn": self._dedup_count,
        }
```

## Solution 3: Deduplicating Tool Call Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class DeduplicatingToolCallDispatcher:
    """
    Dispatches a list of tool calls for a single turn, deduplicating
    any calls whose (tool_name, arguments) fingerprint was already
    dispatched in this turn. Resets dedup state at turn boundaries.
    """

    def __init__(
        self,
        fingerprinter: TurnScopedCallFingerprinter,
        registry: WithinTurnDeduplicationRegistry,
    ):
        self._fp = fingerprinter
        self._registry = registry
        self._lifetime_dedup_hits = 0
        self._lifetime_executions = 0

    def new_turn(self) -> None:
        self._registry.reset()

    async def dispatch_all(
        self,
        calls: List[Dict[str, Any]],
        execute_fn: Callable[[str, Dict[str, Any]], Any],
    ) -> List[dict]:
        fingerprinted = self._fp.fingerprint_list(calls)
        results = []

        for call, fp in fingerprinted:
            name = call.get("name") or call.get("tool_name", "")
            args = call.get("arguments") or call.get("input") or {}
            call_id = call.get("id", fp[:8])

            if self._registry.is_duplicate(fp):
                cached = self._registry.get_result(fp)
                self._registry.record_dedup()
                self._lifetime_dedup_hits += 1
                results.append({
                    "call_id": call_id,
                    "tool_name": name,
                    "result": cached,
                    "deduplicated": True,
                    "fingerprint": fp,
                })
            else:
                start = time.time()
                try:
                    result = await execute_fn(name, args)
                except Exception as exc:
                    result = {"error": str(exc)}
                latency_ms = round((time.time() - start) * 1000, 2)
                self._registry.register(fp, result)
                self._lifetime_executions += 1
                results.append({
                    "call_id": call_id,
                    "tool_name": name,
                    "result": result,
                    "deduplicated": False,
                    "fingerprint": fp,
                    "latency_ms": latency_ms,
                })

        return results

    def lifetime_stats(self) -> dict:
        total = self._lifetime_executions + self._lifetime_dedup_hits
        return {
            "lifetime_executions": self._lifetime_executions,
            "lifetime_dedup_hits": self._lifetime_dedup_hits,
            "lifetime_dedup_rate": round(
                self._lifetime_dedup_hits / max(total, 1), 4
            ),
        }
```

## Solution 4: Semantic Similarity Deduplicator

```python
import math
from typing import Any, Callable, Dict, List, Optional, Tuple


class SemanticSimilarityDeduplicator:
    """
    Extends exact-fingerprint deduplication with a semantic similarity
    check for calls that differ only in minor argument variations
    (e.g., whitespace-normalized query strings).
    Uses cosine similarity on argument value character n-grams.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.95,
        ngram_size: int = 3,
    ):
        self._threshold = similarity_threshold
        self._n = ngram_size
        self._dispatched: List[Tuple[str, Dict[str, Any], Any]] = []
        # (tool_name, arguments, result)

    def _ngrams(self, text: str) -> set:
        t = text.lower()
        return {t[i:i + self._n] for i in range(len(t) - self._n + 1)} if len(t) >= self._n else {t}

    def _jaccard(self, a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        union = len(a | b)
        return len(a & b) / union if union else 0.0

    def _args_similarity(
        self, args_a: Dict[str, Any], args_b: Dict[str, Any]
    ) -> float:
        import json
        text_a = json.dumps(args_a, sort_keys=True)
        text_b = json.dumps(args_b, sort_keys=True)
        return self._jaccard(self._ngrams(text_a), self._ngrams(text_b))

    def find_similar(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[Any]:
        for prev_name, prev_args, prev_result in self._dispatched:
            if prev_name != tool_name:
                continue
            sim = self._args_similarity(arguments, prev_args)
            if sim >= self._threshold:
                return prev_result
        return None

    def register(
        self, tool_name: str, arguments: Dict[str, Any], result: Any
    ) -> None:
        self._dispatched.append((tool_name, arguments, result))

    def reset(self) -> None:
        self._dispatched.clear()
```

## Solution 5: Dedup-Aware Tool Call Orchestrator

```python
from typing import Any, Callable, Dict, List


class DedupAwareToolCallOrchestrator:
    """
    Combines exact-fingerprint deduplication with semantic similarity
    deduplication for a comprehensive within-turn call dedup strategy.
    """

    def __init__(
        self,
        dispatcher: DeduplicatingToolCallDispatcher,
        semantic_dedup: SemanticSimilarityDeduplicator,
    ):
        self._dispatcher = dispatcher
        self._semantic = semantic_dedup

    def new_turn(self) -> None:
        self._dispatcher.new_turn()
        self._semantic.reset()

    async def execute_turn_calls(
        self,
        calls: List[Dict[str, Any]],
        execute_fn: Callable[[str, Dict[str, Any]], Any],
    ) -> dict:
        async def wrapped_execute(name: str, args: Dict[str, Any]) -> Any:
            similar_result = self._semantic.find_similar(name, args)
            if similar_result is not None:
                return similar_result
            result = await execute_fn(name, args)
            self._semantic.register(name, args, result)
            return result

        results = await self._dispatcher.dispatch_all(calls, wrapped_execute)
        exact_dedups = sum(1 for r in results if r["deduplicated"])
        return {
            "results": results,
            "total_calls": len(results),
            "exact_dedup_count": exact_dedups,
            "executed_count": len(results) - exact_dedups,
        }
```

## Solution 6: Within-Turn Dedup Monitor

```python
import time
from typing import List


class WithinTurnDedupMonitor:
    """
    Accumulates per-turn deduplication statistics and surfaces
    patterns that indicate the LLM is habitually issuing duplicate calls.
    """

    def __init__(self):
        self._turn_reports: List[dict] = []
        self._recorded_at: List[float] = []

    def record_turn(self, orchestrator_result: dict) -> None:
        self._turn_reports.append(orchestrator_result)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._turn_reports, self._recorded_at)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "turns": 0}

        total_calls = sum(r["total_calls"] for r in recent)
        total_deduped = sum(r["exact_dedup_count"] for r in recent)
        turns_with_dedup = sum(1 for r in recent if r["exact_dedup_count"] > 0)

        return {
            "window_seconds": window_seconds,
            "turns": len(recent),
            "total_calls": total_calls,
            "total_deduped": total_deduped,
            "dedup_rate": round(total_deduped / max(total_calls, 1), 4),
            "turns_with_duplicates": turns_with_dedup,
            "duplicate_turn_rate": round(turns_with_dedup / max(len(recent), 1), 4),
        }
```

## Comparison

| Approach | Exact Dedup | Semantic Dedup | Turn Reset | Lifetime Stats | Monitor |
|---|---|---|---|---|---|
| TurnScopedCallFingerprinter | Yes (SHA-256) | No | No | No | No |
| WithinTurnDeduplicationRegistry | Via fingerprint | No | Yes | Partial | No |
| DeduplicatingToolCallDispatcher | Yes | No | Via registry | Yes | No |
| SemanticSimilarityDeduplicator | No | Yes (Jaccard) | Yes | No | No |
| DedupAwareToolCallOrchestrator | Via dispatcher | Via semantic | Yes | No | No |
| WithinTurnDedupMonitor | No | No | No | No | Yes |

**Best for production**: Always reset deduplication state at turn boundaries — a call that was legitimately re-issued in a later turn (e.g., re-reading a file after editing it) must not be blocked by a stale fingerprint from a previous turn. Start with exact fingerprint deduplication only; add `SemanticSimilarityDeduplicator` only if profiling shows the LLM frequently issues near-identical calls that differ only in whitespace. Monitor `duplicate_turn_rate` — consistently above 10% indicates the LLM is confused about which tools it has already called and the system prompt should be updated to instruct the model not to repeat tool calls within a single turn.
