---
layout: solution
title: "Agent Stores Redundant Duplicate Memories — Memory Grows Without Bound"
category: memory
description: "Agent saves 'user prefers Python' five times across five sessions. Memory file grows to 10,000 lines. Retrieval returns 12 near-identical entries for the same fact. The agent wastes context on duplicates and eventually the memory store becomes too large to use."
tags: [memory, deduplication, persistence, embeddings, compaction, redundancy, semantic-similarity, long-term-memory]
---

# Agent Stores Redundant Duplicate Memories — Memory Grows Without Bound

## Symptom
- Memory file contains 50 variations of "user works at Acme Corp" from different sessions
- Agent recalls "user prefers Python" — but returns 8 duplicate entries instead of 1
- Memory store grows by hundreds of entries per session — unusable after a week
- Agent saves every user message as a memory — 90% are redundant or irrelevant
- Semantic search returns near-duplicate results — top 5 hits are all the same fact
- Memory retrieval tokens consume half the context before the agent even starts
- Agent contradicts itself — has both "user prefers dark mode" and "user prefers light mode" stored

## Root Cause
Agents write memories without checking if equivalent information already exists. Each session may re-learn the same facts and write them again. Without deduplication, the memory store grows proportionally to usage time rather than to new information. Near-duplicate detection requires semantic similarity comparison — exact string matching misses paraphrases. The fix is to check for existing similar memories before writing, merge updates into existing entries, and periodically compact the store.

## Fix

### Option 1: Semantic deduplication — check similarity before writing

```python
import hashlib
import json
from pathlib import Path
from typing import Optional
import anthropic

client = anthropic.Anthropic()

class DeduplicatingMemoryStore:
    """
    Memory store that checks for semantic duplicates before writing.
    Uses a fast LLM call to determine if a fact is already known.
    """

    def __init__(
        self,
        store_path: str = "/data/memory.json",
        similarity_threshold: float = 0.85,
        check_model: str = "claude-haiku-4-5-20251001"
    ):
        self.store_path = Path(store_path)
        self.similarity_threshold = similarity_threshold
        self.check_model = check_model
        self._memories: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self.store_path.exists():
            return json.loads(self.store_path.read_text())
        return []

    def _save(self):
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._memories, indent=2))
        tmp.replace(self.store_path)

    def _find_duplicate(self, new_fact: str) -> Optional[int]:
        """
        Check if a semantically equivalent fact already exists.
        Returns the index of the duplicate, or None.
        """
        if not self._memories:
            return None

        # Quick exact-match check first (free)
        new_lower = new_fact.lower().strip()
        for i, mem in enumerate(self._memories):
            if mem["content"].lower().strip() == new_lower:
                return i

        # Semantic check against candidate memories (top 20 by relevance)
        # For simplicity here, check all; in production, pre-filter with keywords
        candidates = self._memories[-20:]  # Check most recent 20
        if not candidates:
            return None

        candidate_text = "\n".join(
            f"{i}. {m['content']}"
            for i, m in enumerate(candidates)
        )

        response = client.messages.create(
            model=self.check_model,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"New fact to store: \"{new_fact}\"\n\n"
                    f"Existing facts:\n{candidate_text}\n\n"
                    f"Is the new fact semantically equivalent to any existing fact "
                    f"(same meaning, possibly different wording)?\n"
                    f"If yes, reply with the number (0-based index). "
                    f"If no duplicate, reply: 'none'\n"
                    f"Reply with only a number or 'none'."
                )
            }]
        )

        reply = response.content[0].text.strip().lower()
        if reply == "none":
            return None
        try:
            idx = int(reply)
            if 0 <= idx < len(candidates):
                # Map back to original index
                offset = len(self._memories) - len(candidates)
                return offset + idx
        except ValueError:
            pass
        return None

    def write(
        self,
        content: str,
        category: str = "general",
        merge_updates: bool = True
    ) -> dict:
        """
        Write a memory, deduplicating against existing entries.
        Returns the memory that was written (new or existing).
        """
        duplicate_idx = self._find_duplicate(content)

        if duplicate_idx is not None:
            existing = self._memories[duplicate_idx]

            if merge_updates and content != existing["content"]:
                # Update the existing memory with newer information
                old_content = existing["content"]
                existing["content"] = content
                existing["updated_count"] = existing.get("updated_count", 0) + 1
                existing["previous_versions"] = existing.get("previous_versions", []) + [old_content]
                self._save()
                print(f"Memory updated (was duplicate): '{old_content[:50]}' → '{content[:50]}'")
            else:
                print(f"Memory skipped (duplicate): '{content[:60]}'")

            return existing

        # New memory — write it
        import time
        memory = {
            "id": hashlib.sha256(content.encode()).hexdigest()[:12],
            "content": content,
            "category": category,
            "created_at": time.time(),
            "updated_count": 0
        }
        self._memories.append(memory)
        self._save()
        print(f"Memory stored: '{content[:60]}'")
        return memory

    def read_all(self) -> list[dict]:
        return self._memories

    @property
    def stats(self) -> dict:
        return {
            "total_memories": len(self._memories),
            "categories": list({m.get("category", "general") for m in self._memories}),
            "updated_entries": sum(1 for m in self._memories if m.get("updated_count", 0) > 0)
        }

store = DeduplicatingMemoryStore()
```

### Option 2: Memory compaction — merge and consolidate periodically

```python
import json
import time
from pathlib import Path
import anthropic

client = anthropic.Anthropic()

class MemoryCompactor:
    """
    Periodically compacts a memory store:
    - Merges near-duplicate entries
    - Reconciles contradictions (keeps newest)
    - Removes stale or irrelevant entries
    - Produces a clean, deduplicated store
    """

    def __init__(
        self,
        store_path: str = "/data/memory.json",
        compaction_model: str = "claude-sonnet-4-6"
    ):
        self.store_path = Path(store_path)
        self.compaction_model = compaction_model

    def should_compact(self, memories: list[dict], threshold: int = 100) -> bool:
        """Compact when memory count exceeds threshold"""
        return len(memories) > threshold

    def compact(self, memories: list[dict]) -> list[dict]:
        """
        Compact a list of memories into a deduplicated, reconciled set.
        Uses Claude to identify duplicates and contradictions.
        """
        if len(memories) <= 20:
            return memories  # Small enough — no compaction needed

        print(f"Compacting {len(memories)} memories...")

        # Process in batches of 50
        batch_size = 50
        compacted = []

        for i in range(0, len(memories), batch_size):
            batch = memories[i:i + batch_size]
            compacted_batch = self._compact_batch(batch)
            compacted.extend(compacted_batch)
            print(f"Batch {i // batch_size + 1}: {len(batch)} → {len(compacted_batch)} memories")

        # Final dedup pass across all batches
        if len(compacted) > batch_size:
            compacted = self._compact_batch(compacted)

        print(f"Compaction complete: {len(memories)} → {len(compacted)} memories")
        return compacted

    def _compact_batch(self, memories: list[dict]) -> list[dict]:
        """Compact a single batch of memories"""
        memory_text = "\n".join(
            f"{i}. [{m.get('category', 'general')}] {m['content']}"
            for i, m in enumerate(memories)
        )

        response = client.messages.create(
            model=self.compaction_model,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": (
                    f"Below is a list of agent memories. Many are duplicates or near-duplicates.\n\n"
                    f"Task: Produce a deduplicated list by:\n"
                    f"1. Merging near-duplicate facts into one entry (keep the most complete version)\n"
                    f"2. For contradictions, keep the most specific/recent fact\n"
                    f"3. Removing obviously redundant entries\n"
                    f"4. Preserving all unique facts\n\n"
                    f"Return a JSON array of strings — one string per unique memory.\n"
                    f"Preserve the original wording where possible.\n\n"
                    f"Memories:\n{memory_text}"
                )
            }]
        )

        try:
            import re
            text = response.content[0].text
            # Extract JSON array from response
            match = re.search(r'\[.*?\]', text, re.DOTALL)
            if match:
                compacted_contents = json.loads(match.group())
                return [
                    {
                        "id": hashlib.sha256(c.encode()).hexdigest()[:12],
                        "content": c,
                        "category": "compacted",
                        "compacted_at": time.time()
                    }
                    for c in compacted_contents
                ]
        except Exception as e:
            print(f"Compaction parse error: {e} — returning original batch")

        return memories  # Fallback: return original if parsing fails

    def run(self) -> dict:
        """Run compaction on the store file"""
        if not self.store_path.exists():
            return {"status": "no_store"}

        memories = json.loads(self.store_path.read_text())
        original_count = len(memories)

        if not self.should_compact(memories):
            return {"status": "skipped", "count": original_count}

        compacted = self.compact(memories)

        # Atomic write
        tmp = self.store_path.with_suffix(".compact.tmp")
        tmp.write_text(json.dumps(compacted, indent=2))
        tmp.replace(self.store_path)

        return {
            "status": "compacted",
            "before": original_count,
            "after": len(compacted),
            "reduction": f"{(1 - len(compacted)/original_count)*100:.0f}%"
        }

import hashlib  # needed above
compactor = MemoryCompactor()
# Run compaction periodically (e.g., at agent startup when store is large):
# result = compactor.run()
# print(f"Memory compaction: {result}")
```

### Option 3: Category-based deduplication — one canonical entry per topic

```python
from dataclasses import dataclass, field
from typing import Optional
import time, json, hashlib
from pathlib import Path

MEMORY_CATEGORIES = {
    "user_preference": "One entry per preference topic — replace, don't append",
    "user_identity": "Stable facts about the user — merge, never duplicate",
    "project_context": "Current project state — replace on change",
    "technical_fact": "Technical knowledge — deduplicate by topic",
    "interaction_pattern": "Behavior patterns — aggregate, not duplicate"
}

@dataclass
class CategorizedMemory:
    id: str
    category: str
    topic: str           # Unique key within category
    content: str
    created_at: float
    updated_at: float
    update_count: int = 0

class TopicKeyedMemoryStore:
    """
    Memory store where each category+topic combination has exactly ONE entry.
    Writing to an existing topic replaces (or merges with) the existing entry.
    Eliminates duplicates by design.
    """

    def __init__(self, store_path: str = "/data/keyed_memory.json"):
        self.store_path = Path(store_path)
        self._store: dict[str, CategorizedMemory] = self._load()

    def _key(self, category: str, topic: str) -> str:
        return f"{category}::{topic.lower().strip()}"

    def _load(self) -> dict:
        if not self.store_path.exists():
            return {}
        raw = json.loads(self.store_path.read_text())
        return {k: CategorizedMemory(**v) for k, v in raw.items()}

    def _save(self):
        data = {k: v.__dict__ for k, v in self._store.items()}
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.store_path)

    def write(
        self,
        category: str,
        topic: str,
        content: str,
        merge_fn=None
    ) -> CategorizedMemory:
        """
        Write a memory entry. Topic is unique within category.
        If topic already exists, merges or replaces based on merge_fn.
        """
        key = self._key(category, topic)
        now = time.time()

        if key in self._store:
            existing = self._store[key]
            old_content = existing.content

            if merge_fn:
                # Custom merge logic (e.g., append to a list)
                existing.content = merge_fn(existing.content, content)
            else:
                # Default: replace with newer content
                existing.content = content

            existing.updated_at = now
            existing.update_count += 1
            self._save()
            print(f"Memory updated [{category}::{topic}]: '{old_content[:40]}' → '{existing.content[:40]}'")
            return existing

        # New topic
        memory = CategorizedMemory(
            id=hashlib.sha256(key.encode()).hexdigest()[:12],
            category=category,
            topic=topic,
            content=content,
            created_at=now,
            updated_at=now
        )
        self._store[key] = memory
        self._save()
        print(f"Memory created [{category}::{topic}]: '{content[:60]}'")
        return memory

    def read(self, category: str, topic: str) -> Optional[CategorizedMemory]:
        return self._store.get(self._key(category, topic))

    def read_category(self, category: str) -> list[CategorizedMemory]:
        prefix = f"{category}::"
        return [m for k, m in self._store.items() if k.startswith(prefix)]

    def format_for_context(self, categories: list[str] = None) -> str:
        """Format memories for injection into agent context"""
        if categories:
            memories = []
            for cat in categories:
                memories.extend(self.read_category(cat))
        else:
            memories = list(self._store.values())

        if not memories:
            return ""

        lines = ["## Memory\n"]
        by_category: dict[str, list] = {}
        for m in memories:
            by_category.setdefault(m.category, []).append(m)

        for category, mems in sorted(by_category.items()):
            lines.append(f"### {category.replace('_', ' ').title()}")
            for m in mems:
                lines.append(f"- {m.content}")
            lines.append("")

        return "\n".join(lines)

keyed_store = TopicKeyedMemoryStore()

# Usage — no duplicates possible:
keyed_store.write("user_preference", "programming_language", "User prefers Python over JavaScript")
keyed_store.write("user_preference", "programming_language", "User prefers Python, especially for data work")
# Second write updates the entry — no duplicate created
```

### Option 4: Staleness-based pruning — automatically expire old memories

```python
import time
import json
from pathlib import Path

class StalenessAwareMemoryStore:
    """
    Automatically removes memories that haven't been accessed recently.
    Prevents unbounded growth from rarely-needed facts.
    """

    def __init__(
        self,
        store_path: str = "/data/memory_with_ttl.json",
        default_ttl_days: int = 90,
        max_memories: int = 500
    ):
        self.store_path = Path(store_path)
        self.default_ttl = default_ttl_days * 86400
        self.max_memories = max_memories
        self._memories: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self.store_path.exists():
            return []
        return json.loads(self.store_path.read_text())

    def _save(self):
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._memories, indent=2))
        tmp.replace(self.store_path)

    def write(self, content: str, category: str = "general", ttl_days: int = None):
        import hashlib
        ttl = (ttl_days or 90) * 86400
        now = time.time()
        self._memories.append({
            "id": hashlib.sha256(f"{content}{now}".encode()).hexdigest()[:12],
            "content": content,
            "category": category,
            "created_at": now,
            "last_accessed_at": now,
            "access_count": 0,
            "expires_at": now + ttl
        })
        self._prune()
        self._save()

    def access(self, memory_id: str):
        """Record access — resets staleness timer"""
        for m in self._memories:
            if m["id"] == memory_id:
                m["last_accessed_at"] = time.time()
                m["access_count"] = m.get("access_count", 0) + 1
                # Extend expiry on access
                m["expires_at"] = time.time() + self.default_ttl
                break
        self._save()

    def _prune(self):
        """Remove expired and excess memories"""
        now = time.time()

        # Remove expired
        before = len(self._memories)
        self._memories = [m for m in self._memories if m.get("expires_at", float("inf")) > now]
        expired = before - len(self._memories)
        if expired:
            print(f"Pruned {expired} expired memories")

        # If still over limit, remove least recently accessed
        if len(self._memories) > self.max_memories:
            self._memories.sort(key=lambda m: m.get("last_accessed_at", 0))
            excess = len(self._memories) - self.max_memories
            self._memories = self._memories[excess:]
            print(f"Pruned {excess} least-recently-accessed memories (limit: {self.max_memories})")

    def prune_now(self) -> dict:
        """Run pruning explicitly — call at agent startup"""
        before = len(self._memories)
        self._prune()
        self._save()
        return {"before": before, "after": len(self._memories), "removed": before - len(self._memories)}

store = StalenessAwareMemoryStore(max_memories=200, default_ttl_days=60)
```

### Option 5: Write-gate — decide if a fact is worth storing at all

```python
import anthropic

client = anthropic.Anthropic()

class MemoryWriteGate:
    """
    Before writing any memory, evaluate whether it's worth storing.
    Filters out: ephemeral facts, noise, already-known facts.
    """

    GATE_PROMPT = """Evaluate whether this fact should be stored in long-term memory.

Fact: "{fact}"

Existing memories (sample):
{existing_sample}

Store in long-term memory only if:
- The fact is stable and will be useful across future sessions
- The fact is NOT already captured by an existing memory
- The fact is specific and actionable (not vague or obvious)

Do NOT store:
- Ephemeral facts ("user is currently busy")
- Facts already in existing memories (check for semantic equivalence)
- Obvious facts ("user types on a keyboard")
- Session-specific context ("user asked about X earlier today")

Reply with one word: YES or NO
Then on a new line, briefly explain why (one sentence)."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.model = model

    def should_store(
        self,
        fact: str,
        existing_memories: list[dict],
        sample_size: int = 15
    ) -> tuple[bool, str]:
        """
        Returns (should_store, reason)
        """
        # Sample recent existing memories
        sample = existing_memories[-sample_size:]
        sample_text = "\n".join(f"- {m['content']}" for m in sample) or "(none)"

        response = client.messages.create(
            model=self.model,
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": self.GATE_PROMPT.format(
                    fact=fact,
                    existing_sample=sample_text
                )
            }]
        )

        reply = response.content[0].text.strip()
        lines = reply.split("\n", 1)
        decision = lines[0].strip().upper()
        reason = lines[1].strip() if len(lines) > 1 else ""

        return decision == "YES", reason

gate = MemoryWriteGate()

def gated_memory_write(fact: str, store: DeduplicatingMemoryStore):
    """Only write memory if it passes the write gate"""
    should_store, reason = gate.should_store(fact, store.read_all())
    if should_store:
        store.write(fact)
        print(f"Memory stored: {reason}")
    else:
        print(f"Memory skipped: {reason}")
```

## Memory Growth Failure Modes

| Pattern | Root Cause | Fix |
|---|---|---|
| Same fact stored N times | No duplicate check before write | Semantic dedup before write |
| Growing list of contradictions | Old entries never updated | Topic-keyed store (one per topic) |
| Memory full of session-specific noise | No relevance filter | Write gate — filter ephemeral facts |
| Near-duplicates from paraphrases | Exact-match dedup only | Semantic similarity check |
| Store grows 100+ entries/day | Every message saved | Write gate + category limits |
| Stale facts never cleared | No TTL or pruning | Staleness-based TTL |

## Expected Token Savings
1,000-entry memory injected into every context: ~15,000 tokens overhead per session
200-entry deduplicated store: ~3,000 tokens — 80% savings with higher quality

## Environment
- Any agent with persistent long-term memory across sessions; critical for personal assistants, customer-facing agents, and autonomous agents that accumulate facts over days and weeks — memory bloat is a slow failure that becomes visible only after weeks of operation
- Source: direct experience; unbounded memory growth is the most common long-term stability failure in agents with persistent state
