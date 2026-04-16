---
title: "Agent Doesn't Implement Tiered Context Storage for Long-Running Conversations"
description: "Agents that keep the full conversation history in the context window for long-running sessions eventually hit the model's context limit — at which point they either truncate arbitrarily or fail. Implement tiered context storage with a hot tier (recent messages in context), a warm tier (summarized older exchanges), and a cold tier (archived raw messages) so conversations can continue indefinitely without quality loss."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-tiered-context-storage-for-long-running-conversations
tags: [context-management, tiered-storage, conversation-history, summarization, long-context, memory-management]
symptoms:
  - "Session fails or truncates after 40+ message turns as context window fills"
  - "Early conversation context dropped arbitrarily when limit approaches"
  - "No summarization of old turns — raw message history kept indefinitely in context"
  - "Cannot resume a conversation from the previous day without re-reading all history"
  - "Context window utilization hits 90% after 20 turns — no eviction strategy"
---

## Why This Happens

Context windows are fixed-size buffers. A naive implementation appends every message and eventually overflows. Arbitrary truncation (drop the oldest N messages) loses information without replacement. Tiered storage solves this by promoting context management to a first-class concern: recent messages stay in the hot tier (full fidelity, in-context), older exchanges are compressed into a summary (warm tier, injected as a compact context block), and raw history is archived to the cold tier (retrievable on demand). The model always sees a fixed-size context that combines the warm summary with the hot recent messages.

## Solution 1: Context Tier Configuration

```python
from dataclasses import dataclass
from enum import Enum


class ContextTier(str, Enum):
    HOT = "hot"     # in context, full fidelity
    WARM = "warm"   # summarized, injected as compact block
    COLD = "cold"   # archived, retrievable on demand


@dataclass
class ContextTierConfig:
    hot_max_messages: int = 20          # keep last N messages at full fidelity
    hot_max_tokens: int = 8000          # token budget for hot tier
    warm_summary_max_tokens: int = 1000 # budget for warm summary block
    cold_archive: bool = True           # archive to cold storage
    summarize_batch_size: int = 10      # summarize in batches of N messages
    token_estimate_chars_per: int = 4   # chars per token estimate
```

## Solution 2: Context Message

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ContextMessage:
    role: str        # "user" | "assistant" | "tool"
    content: str
    message_id: str = ""
    turn_index: int = 0
    tier: ContextTier = ContextTier.HOT
    token_estimate: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.message_id:
            import uuid
            self.message_id = uuid.uuid4().hex[:12]
        if self.token_estimate == 0:
            self.token_estimate = max(1, len(self.content) // 4)
```

## Solution 3: Tiered Context Manager

```python
import time
from typing import Any, Callable, List, Optional, Tuple


class TieredContextManager:
    """
    Maintains hot/warm/cold tiers for a conversation session.
    Automatically promotes overflow from hot to warm via summarization
    and archives displaced messages to cold storage.
    """

    def __init__(
        self,
        config: ContextTierConfig,
        summarizer: Callable[[List[ContextMessage]], str],
        cold_archive_fn: Optional[Callable[[List[ContextMessage]], None]] = None,
    ):
        self._config = config
        self._summarize = summarizer
        self._archive = cold_archive_fn
        self._hot: List[ContextMessage] = []
        self._warm_summary: str = ""
        self._warm_token_count: int = 0
        self._cold_count: int = 0
        self._turn_index: int = 0

    def add_message(self, role: str, content: str, metadata: dict = None) -> ContextMessage:
        self._turn_index += 1
        msg = ContextMessage(
            role=role,
            content=content,
            turn_index=self._turn_index,
            metadata=metadata or {},
        )
        self._hot.append(msg)
        self._maybe_evict()
        return msg

    def _hot_token_count(self) -> int:
        return sum(m.token_estimate for m in self._hot)

    def _maybe_evict(self) -> None:
        while (
            len(self._hot) > self._config.hot_max_messages
            or self._hot_token_count() > self._config.hot_max_tokens
        ):
            batch_size = min(self._config.summarize_batch_size, len(self._hot) // 2)
            if batch_size < 1:
                break
            to_evict = self._hot[:batch_size]
            self._hot = self._hot[batch_size:]
            self._absorb_into_warm(to_evict)

    def _absorb_into_warm(self, messages: List[ContextMessage]) -> None:
        new_summary = self._summarize(messages)
        if self._warm_summary:
            combined = f"{self._warm_summary}\n\n{new_summary}"
        else:
            combined = new_summary

        token_est = max(1, len(combined) // 4)
        if token_est > self._config.warm_summary_max_tokens:
            # Re-summarize the combined summary
            summary_msg = [ContextMessage(role="system", content=combined)]
            combined = self._summarize(summary_msg)

        self._warm_summary = combined
        self._warm_token_count = max(1, len(combined) // 4)

        if self._archive:
            for m in messages:
                m.tier = ContextTier.COLD
            self._archive(messages)
            self._cold_count += len(messages)

    def build_context(self) -> Tuple[Optional[str], List[dict]]:
        """
        Returns (warm_summary_or_None, hot_messages_as_dicts).
        Caller injects warm_summary as a system prefix before hot messages.
        """
        hot_dicts = [{"role": m.role, "content": m.content} for m in self._hot]
        return self._warm_summary or None, hot_dicts

    def stats(self) -> dict:
        return {
            "hot_messages": len(self._hot),
            "hot_tokens_est": self._hot_token_count(),
            "warm_tokens_est": self._warm_token_count,
            "cold_archived": self._cold_count,
            "total_turns": self._turn_index,
        }
```

## Solution 4: Cold Archive Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import List, Optional


class ColdArchiveStore:
    """
    Archives evicted messages to a JSONL file per session.
    Supports retrieval by turn range for on-demand context loading.
    """

    def __init__(self, archive_dir: str = "/tmp/context_archive"):
        self._dir = Path(archive_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def archive(self, session_id: str, messages: List[ContextMessage]) -> None:
        path = self._dir / f"{session_id}.jsonl"
        with self._lock:
            with open(path, "a") as f:
                for m in messages:
                    f.write(json.dumps({
                        "message_id": m.message_id,
                        "role": m.role,
                        "content": m.content,
                        "turn_index": m.turn_index,
                        "created_at": m.created_at,
                    }) + "\n")

    def retrieve(
        self,
        session_id: str,
        from_turn: int = 0,
        to_turn: Optional[int] = None,
    ) -> List[dict]:
        path = self._dir / f"{session_id}.jsonl"
        if not path.exists():
            return []
        results = []
        with self._lock:
            with open(path) as f:
                for line in f:
                    try:
                        record = json.loads(line)
                        ti = record.get("turn_index", 0)
                        if ti >= from_turn and (to_turn is None or ti <= to_turn):
                            results.append(record)
                    except json.JSONDecodeError:
                        pass
        return results
```

## Solution 5: Context Utilization Monitor

```python


class ContextUtilizationMonitor:
    """
    Reports context tier utilization as a fraction of configured limits.
    Alerts when warm summary is growing faster than expected.
    """

    def __init__(self, manager: TieredContextManager, config: ContextTierConfig):
        self._manager = manager
        self._config = config

    def report(self) -> dict:
        stats = self._manager.stats()
        hot_utilization = stats["hot_tokens_est"] / max(self._config.hot_max_tokens, 1)
        warm_utilization = stats["warm_tokens_est"] / max(self._config.warm_summary_max_tokens, 1)

        alerts = []
        if hot_utilization > 0.90:
            alerts.append("hot tier near capacity — eviction imminent")
        if warm_utilization > 0.90:
            alerts.append("warm summary near capacity — re-summarization will occur")

        return {
            "hot_utilization_pct": round(hot_utilization * 100, 1),
            "warm_utilization_pct": round(warm_utilization * 100, 1),
            "stats": stats,
            "alerts": alerts,
        }
```

## Solution 6: Tiered Context Dashboard

```python
import time


class TieredContextDashboard:
    def __init__(
        self,
        manager: TieredContextManager,
        monitor: ContextUtilizationMonitor,
    ):
        self._manager = manager
        self._monitor = monitor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "utilization": self._monitor.report(),
        }
```

## Comparison

| Approach | Hot Tier Management | Warm Summarization | Cold Archival | Utilization Monitoring | Dashboard |
|---|---|---|---|---|---|
| TieredContextManager | Yes (FIFO eviction) | Yes (pluggable fn) | Via archive_fn | No | No |
| ColdArchiveStore | No | No | Yes (JSONL) | No | No |
| ContextUtilizationMonitor | Via manager | No | No | Yes | No |
| TieredContextDashboard | No | No | No | Via monitor | No |

**Best for production**: Use an LLM call as the `summarizer` function — pass the evicted batch to the model with the instruction "summarize this conversation segment in 200 words preserving key facts and decisions." Cache summarizer outputs keyed by message IDs to avoid re-summarizing the same batch on restart. Set `hot_max_messages=20` and `warm_summary_max_tokens=1000` as starting points; adjust based on your model's context window and average message length. Monitor `warm_utilization_pct`: above 80% means the warm summary is accumulating detail faster than it is being compressed — reduce `summarize_batch_size` to trigger more aggressive compression earlier.
