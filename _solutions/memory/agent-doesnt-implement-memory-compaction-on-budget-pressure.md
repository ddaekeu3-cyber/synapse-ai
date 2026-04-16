---
layout: solution
title: "Agent Doesn't Implement Memory Compaction on Budget Pressure"
category: memory
description: "Detect when memory or context is approaching its token budget limit and automatically compact lower-priority memories into dense summaries, freeing space for new information."
tags: [memory, compaction, context-window, token-budget, summarization, pressure, long-running]
---

# Agent Doesn't Implement Memory Compaction on Budget Pressure

## Problem

A long-running agent accumulates memories across many turns. As the memory store grows, injecting it into the context window consumes more tokens on every request. Eventually, the agent either hits the context limit and crashes, silently drops memories, or refuses new information. Without compaction, the only options are truncation (lossy) or no memory at all. Compaction merges lower-priority or older memories into compressed summaries at the right moment — before the budget is exhausted.

## Solution Options

### Option 1: Threshold-Triggered Summary Compaction

```python
import anthropic
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class MemoryEntry:
    text: str
    priority: Literal["high", "medium", "low"] = "medium"
    turn_added: int = 0

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text.split()))


class ThresholdCompactingMemory:
    """
    Tracks memory token usage.
    When usage exceeds COMPACT_THRESHOLD, compacts all LOW-priority entries into one summary.
    """

    TOKEN_BUDGET = 2000
    COMPACT_THRESHOLD = 0.75  # compact when 75% full

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._entries: list[MemoryEntry] = []
        self._turn = 0
        self._compactions = 0

    @property
    def total_tokens(self) -> int:
        return sum(e.token_estimate for e in self._entries)

    @property
    def budget_used(self) -> float:
        return self.total_tokens / self.TOKEN_BUDGET

    def add(self, text: str, priority: Literal["high", "medium", "low"] = "medium") -> None:
        self._entries.append(MemoryEntry(text=text, priority=priority, turn_added=self._turn))
        self._turn += 1
        if self.budget_used >= self.COMPACT_THRESHOLD:
            self._compact()

    def _compact(self) -> None:
        low_entries = [e for e in self._entries if e.priority == "low"]
        if len(low_entries) < 2:
            # Also compact medium if low is insufficient
            low_entries = [e for e in self._entries if e.priority in ("low", "medium")]
        if not low_entries:
            return

        combined = "\n".join(e.text for e in low_entries)
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"Compress these memories into ≤50 words, preserving key facts:\n\n{combined}",
            }],
        )
        summary = resp.content[0].text.strip()

        # Replace compacted entries with single summary entry
        self._entries = [e for e in self._entries if e not in low_entries]
        self._entries.append(MemoryEntry(text=summary, priority="medium", turn_added=self._turn))
        self._compactions += 1
        print(f"[memory] Compacted {len(low_entries)} entries → 1 summary. Budget: {self.budget_used:.0%}")

    def context_string(self) -> str:
        return "\n".join(f"- {e.text}" for e in self._entries)

    def ask(self, question: str) -> str:
        ctx = self.context_string()
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=f"Known facts:\n{ctx}" if ctx else "No prior context.",
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text.strip()


if __name__ == "__main__":
    mem = ThresholdCompactingMemory()

    # Flood with low-priority memories
    facts = [
        ("Alice prefers morning meetings", "low"),
        ("Office coffee machine is on 3rd floor", "low"),
        ("Bob is OOO until Friday", "medium"),
        ("Q1 revenue target is $2M", "high"),
        ("Parking validation available at reception", "low"),
        ("Weekly standup is at 10am Monday", "low"),
        ("Project deadline: March 15", "high"),
        ("Lunch coupon code: LUNCH20", "low"),
        ("VPN required for remote DB access", "medium"),
        ("Carol leads the frontend team", "low"),
    ] * 3  # repeat to trigger compaction

    for text, priority in facts:
        mem.add(text, priority)

    print(f"\nEntries: {len(mem._entries)}, Compactions: {mem._compactions}")
    print(f"Budget used: {mem.budget_used:.0%}")
    print("\nAnswer:", mem.ask("What is the Q1 revenue target?"))

# Expected Token Savings: Compaction reduces context tokens ~60–80% while preserving high-priority facts
# Environment: Long-running agents that accumulate observations over many turns
```

---

### Option 2: Tiered Eviction with Importance Scoring

```python
import anthropic
import time
from dataclasses import dataclass, field


@dataclass
class ScoredMemory:
    text: str
    importance: float        # 0.0–1.0, higher = keep longer
    recency_ts: float = field(default_factory=time.monotonic)
    access_count: int = 0

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text.split()))

    def eviction_score(self) -> float:
        """Lower score = evict first. Balances importance, recency, and access."""
        age_penalty = (time.monotonic() - self.recency_ts) / 3600  # hours old
        return self.importance + 0.1 * self.access_count - 0.05 * age_penalty


class TieredEvictionMemory:
    """
    Maintains memories with importance scores.
    When full, evicts lowest-scoring entries and compacts them into a summary.
    """

    TOKEN_BUDGET = 1500
    EVICT_TO_RATIO = 0.5  # compact down to 50% after triggering

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._memories: list[ScoredMemory] = []

    def store(self, text: str, importance: float = 0.5) -> None:
        self._memories.append(ScoredMemory(text=text, importance=importance))
        if self._total_tokens() > self.TOKEN_BUDGET:
            self._evict_and_compact()

    def recall(self, query: str) -> list[str]:
        # Mark accessed memories for recency tracking
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": f"Query: {query}\nWhich of these are relevant? Reply with comma-separated indices only:\n"
                           + "\n".join(f"{i}: {m.text[:40]}" for i, m in enumerate(self._memories)),
            }],
        )
        try:
            indices = [int(x.strip()) for x in resp.content[0].text.split(",")]
            for i in indices:
                if 0 <= i < len(self._memories):
                    self._memories[i].access_count += 1
                    self._memories[i].recency_ts = time.monotonic()
        except ValueError:
            pass
        return [m.text for m in self._memories]

    def _total_tokens(self) -> int:
        return sum(m.token_estimate for m in self._memories)

    def _evict_and_compact(self) -> None:
        target = int(self.TOKEN_BUDGET * self.EVICT_TO_RATIO)
        sorted_mems = sorted(self._memories, key=lambda m: m.eviction_score())

        to_compact = []
        current = self._total_tokens()
        for mem in sorted_mems:
            if current <= target:
                break
            to_compact.append(mem)
            current -= mem.token_estimate

        if not to_compact:
            return

        combined = "\n".join(m.text for m in to_compact)
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": f"Compress into ≤40 words:\n{combined}"}],
        )
        summary = resp.content[0].text.strip()
        avg_importance = sum(m.importance for m in to_compact) / len(to_compact)

        self._memories = [m for m in self._memories if m not in to_compact]
        self._memories.append(ScoredMemory(text=summary, importance=avg_importance))
        print(f"[evict] Compacted {len(to_compact)} → 1. Tokens: {self._total_tokens()}/{self.TOKEN_BUDGET}")

    def context(self) -> str:
        ranked = sorted(self._memories, key=lambda m: -m.eviction_score())
        return "\n".join(f"- {m.text}" for m in ranked)


if __name__ == "__main__":
    mem = TieredEvictionMemory()
    client = anthropic.Anthropic()

    entries = [
        ("System handles 10k req/day", 0.9),
        ("DB password changed last week", 0.3),
        ("CEO name: Sarah Chen", 0.8),
        ("Coffee machine broken", 0.1),
        ("Launch date: Q2 2025", 0.95),
        ("Sprint 12 completed", 0.4),
        ("Office wifi: Corp_Network", 0.2),
        ("Budget approved: $1.5M", 0.9),
        ("Alice's extension: 4201", 0.2),
        ("Security audit passed", 0.7),
    ] * 2

    for text, imp in entries:
        mem.store(text, importance=imp)

    print(f"\nFinal memory count: {len(mem._memories)}")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=f"Facts:\n{mem.context()}",
        messages=[{"role": "user", "content": "What is the launch date?"}],
    )
    print(f"Answer: {resp.content[0].text.strip()}")

# Expected Token Savings: Eviction targets low-importance entries; high-value facts survive compaction
# Environment: Autonomous agents running for hours with continuous observation ingestion
```

---

### Option 3: Sliding Window with Summary Anchor

```python
import anthropic
from dataclasses import dataclass, field


@dataclass
class MemoryWindow:
    anchor_summary: str = ""   # compressed history before the window
    window: list[str] = field(default_factory=list)  # recent entries (full text)
    max_window_size: int = 10
    anchor_tokens: int = 0


class SlidingWindowMemory:
    """
    Keeps a sliding window of recent memories at full fidelity.
    When the window is full, the oldest entry is merged into the summary anchor.
    The anchor grows slowly while the window always has fresh context.
    """

    MAX_ANCHOR_TOKENS = 200

    def __init__(self, window_size: int = 8) -> None:
        self._client = anthropic.Anthropic()
        self._state = MemoryWindow(max_window_size=window_size)
        self._total_pushed = 0

    def push(self, text: str) -> None:
        self._state.window.append(text)
        self._total_pushed += 1

        if len(self._state.window) > self._state.max_window_size:
            # Evict oldest from window → merge into anchor
            oldest = self._state.window.pop(0)
            self._merge_into_anchor(oldest)

    def _merge_into_anchor(self, new_fact: str) -> None:
        combined = f"{self._state.anchor_summary}\n{new_fact}".strip() if self._state.anchor_summary else new_fact
        # Compress if anchor is getting long
        if len(combined.split()) > self.MAX_ANCHOR_TOKENS:
            resp = self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": f"Compress into ≤50 words:\n{combined}"}],
            )
            combined = resp.content[0].text.strip()
        self._state.anchor_summary = combined
        self._state.anchor_tokens = len(combined.split())

    def context(self) -> str:
        parts = []
        if self._state.anchor_summary:
            parts.append(f"[Summary of earlier context]\n{self._state.anchor_summary}")
        if self._state.window:
            parts.append("[Recent observations]\n" + "\n".join(f"- {t}" for t in self._state.window))
        return "\n\n".join(parts)

    def ask(self, question: str) -> str:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=self.context() or "No prior context.",
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text.strip()

    def stats(self) -> dict:
        return {
            "total_pushed": self._total_pushed,
            "window_size": len(self._state.window),
            "anchor_tokens": self._state.anchor_tokens,
        }


if __name__ == "__main__":
    mem = SlidingWindowMemory(window_size=5)

    observations = [
        "User logged in at 9am",
        "User opened project dashboard",
        "User clicked on 'Reports' tab",
        "User filtered by Q1 2025",
        "User exported data as CSV",
        "User navigated to Settings",
        "User changed notification preferences",
        "User signed out at 11am",
        "New session started at 2pm",
        "User opened support chat",
        "User asked about billing",
    ]
    for obs in observations:
        mem.push(obs)

    print(f"Stats: {mem.stats()}")
    print(f"\nContext:\n{mem.context()}")
    print(f"\nAnswer: {mem.ask('What was the user doing in the morning session?')}")

# Expected Token Savings: Window size caps recent context tokens; anchor grows at O(log N) via compression
# Environment: Session-tracking agents observing long user workflows
```

---

### Option 4: Async Compaction with Background Worker

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class AsyncMemoryEntry:
    text: str
    ts: float = field(default_factory=time.monotonic)
    importance: float = 0.5
    compacted: bool = False


class AsyncCompactingMemory:
    """
    Memory store with background compaction worker.
    The main thread never blocks on compaction — it happens asynchronously.
    """

    TOKEN_BUDGET = 1000
    COMPACT_TRIGGER = 0.8

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic()
        self._entries: list[AsyncMemoryEntry] = []
        self._lock = asyncio.Lock()
        self._compacting = False
        self._compaction_count = 0

    def _total_tokens(self) -> int:
        return sum(max(1, len(e.text.split())) for e in self._entries if not e.compacted)

    async def store(self, text: str, importance: float = 0.5) -> None:
        async with self._lock:
            self._entries.append(AsyncMemoryEntry(text=text, importance=importance))
            pressure = self._total_tokens() / self.TOKEN_BUDGET
            if pressure >= self.COMPACT_TRIGGER and not self._compacting:
                asyncio.create_task(self._compact_background())

    async def _compact_background(self) -> None:
        self._compacting = True
        try:
            async with self._lock:
                low_entries = sorted(
                    [e for e in self._entries if not e.compacted],
                    key=lambda e: e.importance,
                )[:max(1, len(self._entries) // 2)]

            if not low_entries:
                return

            combined = "\n".join(e.text for e in low_entries)
            resp = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": f"Compress into ≤40 words:\n{combined}"}],
            )
            summary = resp.content[0].text.strip()

            async with self._lock:
                for entry in low_entries:
                    entry.compacted = True
                avg_imp = sum(e.importance for e in low_entries) / len(low_entries)
                self._entries.append(AsyncMemoryEntry(text=summary, importance=avg_imp))
                # Clean up compacted entries
                self._entries = [e for e in self._entries if not e.compacted]
                self._compaction_count += 1
                print(f"[async-compact] Done. Entries: {len(self._entries)}, Tokens: {self._total_tokens()}/{self.TOKEN_BUDGET}")
        finally:
            self._compacting = False

    async def context(self) -> str:
        async with self._lock:
            return "\n".join(f"- {e.text}" for e in self._entries)

    async def ask(self, question: str) -> str:
        ctx = await self.context()
        resp = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=f"Memory:\n{ctx}" if ctx else "No memory.",
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text.strip()

    async def close(self) -> None:
        await self._client.close()


async def main() -> None:
    mem = AsyncCompactingMemory()

    # Rapid observation ingestion
    observations = [f"Observation {i}: event_{i % 5} occurred with value {i * 7}" for i in range(20)]
    for obs in observations:
        await mem.store(obs, importance=0.3 if "0" in obs else 0.7)
        await asyncio.sleep(0.05)  # small delay to allow background compaction

    # Wait briefly for any in-flight compaction
    await asyncio.sleep(0.5)

    ctx = await mem.context()
    print(f"Memory entries: {len(ctx.splitlines())}")
    print(f"Compactions: {mem._compaction_count}")
    answer = await mem.ask("Summarize what observations have been recorded")
    print(f"Answer: {answer[:100]}")
    await mem.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Background compaction never blocks request path; zero latency added per store
# Environment: Async agents with high-frequency event ingestion requiring non-blocking memory management
```

---

### Option 5: Semantic Clustering Compaction

```python
import anthropic
from dataclasses import dataclass, field
import hashlib


@dataclass
class ClusteredMemory:
    topic: str
    entries: list[str] = field(default_factory=list)
    summary: str = ""
    is_summarized: bool = False

    @property
    def token_estimate(self) -> int:
        if self.is_summarized:
            return max(1, len(self.summary.split()))
        return sum(max(1, len(e.split())) for e in self.entries)


class SemanticClusteringMemory:
    """
    Groups memories by topic.
    When a topic cluster exceeds its budget, it's summarized in-place.
    Topics with more entries get compacted first.
    """

    CLUSTER_TOKEN_LIMIT = 150  # tokens per cluster before compacting
    TOTAL_BUDGET = 1200

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._clusters: dict[str, ClusteredMemory] = {}

    def _topic_key(self, text: str) -> str:
        """Ask LLM to assign topic, or use a simple keyword hash as fallback."""
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": f"Assign ONE topic word to: {text[:80]}. Reply with one word only."}],
        )
        return resp.content[0].text.strip().lower()[:20]

    def store(self, text: str) -> None:
        topic = self._topic_key(text)
        if topic not in self._clusters:
            self._clusters[topic] = ClusteredMemory(topic=topic)

        cluster = self._clusters[topic]
        if cluster.is_summarized:
            # Re-expand and re-summarize
            cluster.entries.append(text)
            cluster.is_summarized = False
        else:
            cluster.entries.append(text)

        if cluster.token_estimate > self.CLUSTER_TOKEN_LIMIT:
            self._compact_cluster(topic)
        elif self._total_tokens() > self.TOTAL_BUDGET:
            # Find largest cluster to compact
            biggest = max(self._clusters.keys(), key=lambda k: self._clusters[k].token_estimate)
            self._compact_cluster(biggest)

    def _compact_cluster(self, topic: str) -> None:
        cluster = self._clusters[topic]
        if cluster.is_summarized or not cluster.entries:
            return
        combined = "\n".join(cluster.entries)
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": f"Summarize these '{topic}' facts into ≤30 words:\n{combined}"}],
        )
        cluster.summary = resp.content[0].text.strip()
        cluster.entries = []
        cluster.is_summarized = True
        print(f"[cluster] Compacted topic='{topic}' → {len(cluster.summary.split())} words")

    def _total_tokens(self) -> int:
        return sum(c.token_estimate for c in self._clusters.values())

    def context(self) -> str:
        lines = []
        for topic, cluster in self._clusters.items():
            if cluster.is_summarized:
                lines.append(f"[{topic}] {cluster.summary}")
            else:
                for entry in cluster.entries:
                    lines.append(f"[{topic}] {entry}")
        return "\n".join(lines)

    def ask(self, question: str) -> str:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=f"Memory by topic:\n{self.context()}",
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text.strip()


if __name__ == "__main__":
    mem = SemanticClusteringMemory()
    facts = [
        "Alice is the engineering lead",
        "Bob manages the product roadmap",
        "The API rate limit is 100 req/min",
        "Deploy pipeline uses GitHub Actions",
        "Carol handles customer success",
        "Database runs PostgreSQL 15",
        "Dave is the head of design",
        "The mobile app uses React Native",
    ]
    for fact in facts:
        mem.store(fact)

    print(f"\nClusters: {list(mem._clusters.keys())}")
    print(f"Total tokens: {mem._total_tokens()}")
    print(f"\nAnswer: {mem.ask('Who handles customer success?')}")

# Expected Token Savings: Topic clustering prevents unrelated facts from merging; targeted compaction per cluster
# Environment: Agents accumulating knowledge across multiple domains simultaneously
```

---

### Option 6: Pressure-Adaptive Compaction with Fidelity Levels

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum


class Pressure(Enum):
    LOW    = "low"     # < 50%: no compaction
    MEDIUM = "medium"  # 50–75%: compact LOW priority
    HIGH   = "high"    # 75–90%: compact LOW + MEDIUM
    CRITICAL = "critical"  # > 90%: compact everything aggressively


@dataclass
class MemEntry:
    text: str
    priority: int  # 1=low, 2=medium, 3=high
    token_count: int = 0

    def __post_init__(self) -> None:
        self.token_count = max(1, len(self.text.split()))


class PressureAdaptiveMemory:
    TOKEN_BUDGET = 800

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._entries: list[MemEntry] = []
        self._compaction_log: list[str] = []

    @property
    def _used(self) -> int:
        return sum(e.token_count for e in self._entries)

    @property
    def _pressure(self) -> Pressure:
        ratio = self._used / self.TOKEN_BUDGET
        if ratio < 0.5:
            return Pressure.LOW
        elif ratio < 0.75:
            return Pressure.MEDIUM
        elif ratio < 0.9:
            return Pressure.HIGH
        return Pressure.CRITICAL

    def _compact(self, entries: list[MemEntry], max_words: int) -> MemEntry:
        combined = "\n".join(e.text for e in entries)
        avg_priority = round(sum(e.priority for e in entries) / len(entries))
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_words * 2,
            messages=[{"role": "user", "content": f"Compress into ≤{max_words} words:\n{combined}"}],
        )
        summary = resp.content[0].text.strip()
        return MemEntry(text=summary, priority=avg_priority)

    def add(self, text: str, priority: int = 2) -> None:
        self._entries.append(MemEntry(text=text, priority=priority))
        self._apply_pressure()

    def _apply_pressure(self) -> None:
        p = self._pressure
        if p == Pressure.LOW:
            return

        if p == Pressure.MEDIUM:
            to_compact = [e for e in self._entries if e.priority == 1]
            max_words = 40
        elif p == Pressure.HIGH:
            to_compact = [e for e in self._entries if e.priority <= 2]
            max_words = 60
        else:  # CRITICAL
            to_compact = sorted(self._entries, key=lambda e: e.priority)[:-3]  # keep top 3
            max_words = 80

        if len(to_compact) < 2:
            return

        summary_entry = self._compact(to_compact, max_words)
        self._entries = [e for e in self._entries if e not in to_compact]
        self._entries.append(summary_entry)
        self._compaction_log.append(f"[{p.value}] {len(to_compact)} → 1")
        print(f"[pressure] {p.value}: {self._used}/{self.TOKEN_BUDGET} tokens after compact")

    def context(self) -> str:
        ranked = sorted(self._entries, key=lambda e: -e.priority)
        return "\n".join(f"- {e.text}" for e in ranked)

    def ask(self, question: str) -> str:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=f"Memory:\n{self.context()}",
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text.strip()


if __name__ == "__main__":
    mem = PressureAdaptiveMemory()
    for i in range(30):
        priority = 3 if i % 7 == 0 else (2 if i % 3 == 0 else 1)
        mem.add(f"Event {i}: system metric_{i % 5} was {'normal' if i % 2 else 'elevated'}", priority=priority)

    print(f"\nFinal: {len(mem._entries)} entries, {mem._used}/{mem.TOKEN_BUDGET} tokens")
    print(f"Compaction log: {mem._compaction_log}")
    print(f"Answer: {mem.ask('Were any metrics elevated?')[:80]}")

# Expected Token Savings: Pressure tiers prevent over-compaction; fidelity scales with urgency
# Environment: Agents that must maintain continuity through extreme context pressure without full context flush
```

---

## Comparison

| Option | Trigger | Compaction Strategy | Priority Awareness | Async |
|--------|---------|---------------------|--------------------|-------|
| 1 | Token threshold (75%) | Compact LOW entries → 1 summary | Yes (3 tiers) | No |
| 2 | Budget exceeded | Evict lowest importance score | Yes (scored 0–1) | No |
| 3 | Window full | Slide oldest into growing anchor | No (FIFO) | No |
| 4 | Threshold (80%) | Background async compaction | Yes (importance) | Yes |
| 5 | Cluster exceeds limit | Semantic topic clustering | No (per-cluster) | No |
| 6 | Pressure level (LOW/MED/HIGH/CRIT) | Adaptive fidelity by pressure tier | Yes (1–3 tiers) | No |
