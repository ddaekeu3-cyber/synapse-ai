---
layout: solution
title: "Agent Overwrites Memory Without Conflict Resolution"
category: memory
description: "Two concurrent agent instances write conflicting facts to the same memory key — the last writer silently wins, discarding the other's update. Concurrent writes corrupt shared agent state with no error or audit trail."
tags: [memory, concurrency, conflict-resolution, multi-agent, consistency]
---

## Symptom

Two agents both update the `user_preferences` memory key at the same time:

```
Agent A: reads  user_preferences → {"theme": "dark", "language": "en"}
Agent B: reads  user_preferences → {"theme": "dark", "language": "en"}
Agent A: writes user_preferences → {"theme": "light", "language": "en"}   ← A's update
Agent B: writes user_preferences → {"theme": "dark",  "language": "fr"}   ← B's update, OVERWRITES A
```

Agent A's theme change is silently lost. Neither agent raised an error. The user sees their setting revert unexpectedly.

## Root Cause

Memory writes are not atomic with respect to reads. Agents perform a read-modify-write cycle without any version check:

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: read-modify-write without version check
def update_memory(store: dict, key: str, new_data: dict) -> None:
    current = store.get(key, {})
    current.update(new_data)
    store[key] = current  # ← Blind overwrite; concurrent agent sees stale data
```

In a multi-agent system, any two agents running concurrently can interleave their reads and writes, producing lost updates.

---

## Fix

### Option 1 — Optimistic locking with version numbers

Attach a version counter to every memory value. Writers include the version they read; the store rejects writes where the version is stale.

```python
import anthropic
import json
import threading

client = anthropic.Anthropic(api_key="sk-live-...")


class VersionedMemoryStore:
    """Thread-safe memory store with optimistic locking."""

    def __init__(self):
        self._store: dict[str, dict] = {}  # key → {"value": ..., "version": int}
        self._lock = threading.Lock()

    def read(self, key: str) -> tuple[dict, int]:
        """Returns (value, version). Version is 0 if key doesn't exist."""
        with self._lock:
            entry = self._store.get(key, {"value": {}, "version": 0})
            return entry["value"], entry["version"]

    def write(self, key: str, new_value: dict, expected_version: int) -> bool:
        """Write new_value only if current version == expected_version. Returns success."""
        with self._lock:
            entry = self._store.get(key, {"value": {}, "version": 0})
            if entry["version"] != expected_version:
                return False  # Stale read — conflict detected
            self._store[key] = {"value": new_value, "version": expected_version + 1}
            return True

    def write_with_retry(self, key: str, updater, max_retries: int = 5) -> dict:
        """Read-modify-write with automatic retry on conflict."""
        for attempt in range(max_retries):
            value, version = self.read(key)
            new_value = updater(value)
            if self.write(key, new_value, version):
                return new_value
            print(f"[optimistic] Conflict on '{key}' (attempt {attempt + 1}) — retrying")
        raise RuntimeError(f"Could not write '{key}' after {max_retries} retries")


store = VersionedMemoryStore()


def agent_update_preferences(agent_id: str, updates: dict) -> None:
    def updater(current: dict) -> dict:
        merged = dict(current)
        merged.update(updates)
        return merged

    result = store.write_with_retry("user_preferences", updater)
    print(f"[{agent_id}] Wrote: {result}")


# Simulate concurrent agents
t1 = threading.Thread(target=agent_update_preferences, args=("Agent-A", {"theme": "light"}))
t2 = threading.Thread(target=agent_update_preferences, args=("Agent-B", {"language": "fr"}))
t1.start(); t2.start()
t1.join(); t2.join()

final, version = store.read("user_preferences")
print(f"Final (v{version}): {final}")
# → Both updates preserved: {"theme": "light", "language": "fr"}

# Expected Token Savings: conflicts caught at write time → no silent data loss, no re-explaining lost settings
# Environment: multi-agent systems sharing a key-value memory store
```

---

### Option 2 — Last-write-wins with conflict detection and audit log

Accept last-write-wins semantics but detect and log conflicts so they can be reviewed or replayed.

```python
import anthropic
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class MemoryEntry:
    value: dict
    version: int
    agent_id: str
    timestamp: float


@dataclass
class ConflictRecord:
    key: str
    winner: MemoryEntry
    loser: MemoryEntry
    detected_at: float = field(default_factory=time.monotonic)


class AuditedMemoryStore:
    def __init__(self):
        self._store: dict[str, MemoryEntry] = {}
        self._conflicts: list[ConflictRecord] = []
        self._lock = threading.Lock()

    def read(self) -> tuple[dict, int]:
        pass  # not used directly

    def write(self, key: str, value: dict, based_on_version: int, agent_id: str) -> bool:
        with self._lock:
            current = self._store.get(key)

            if current is None:
                self._store[key] = MemoryEntry(value, 1, agent_id, time.monotonic())
                return True

            if current.version != based_on_version:
                # Conflict: current has been updated since we read it
                new_entry = MemoryEntry(value, current.version + 1, agent_id, time.monotonic())
                conflict = ConflictRecord(key=key, winner=new_entry, loser=current)
                self._conflicts.append(conflict)
                print(f"[audit] CONFLICT on '{key}': {current.agent_id} v{current.version} "
                      f"overwritten by {agent_id}")
                self._store[key] = new_entry  # Last-write-wins, but conflict recorded
                return True  # Write succeeded (with conflict)

            self._store[key] = MemoryEntry(value, current.version + 1, agent_id, time.monotonic())
            return True

    def get(self, key: str) -> tuple[dict, int]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return {}, 0
            return entry.value, entry.version

    def conflict_report(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "key": c.key,
                    "lost_agent": c.loser.agent_id,
                    "lost_value": c.loser.value,
                    "winner_agent": c.winner.agent_id,
                    "winner_value": c.winner.value,
                    "detected_at": datetime.fromtimestamp(c.detected_at).isoformat()
                }
                for c in self._conflicts
            ]


store = AuditedMemoryStore()

def agent_write(agent_id: str, key: str, updates: dict) -> None:
    current, version = store.get(key)
    time.sleep(0.01)  # Simulate processing time (race window)
    new_value = {**current, **updates}
    store.write(key, new_value, version, agent_id)


t1 = threading.Thread(target=agent_write, args=("Agent-A", "session", {"step": "login", "user": "alice"}))
t2 = threading.Thread(target=agent_write, args=("Agent-B", "session", {"step": "checkout", "cart": [1, 2]}))
t1.start(); t2.start()
t1.join(); t2.join()

final, version = store.get("session")
print(f"Final v{version}: {final}")
print("Conflicts:")
for conflict in store.conflict_report():
    print(f"  {conflict}")

# Expected Token Savings: conflict log provides audit trail; engineers can replay lost writes
# Environment: production multi-agent systems where LWW is acceptable but visibility is required
```

---

### Option 3 — Merge-based conflict resolution using Claude

When a write conflict is detected, use Claude to merge the conflicting values intelligently based on semantic content.

```python
import anthropic
import json
import threading

client = anthropic.Anthropic(api_key="sk-live-...")


def llm_merge(key: str, base: dict, version_a: dict, version_b: dict) -> dict:
    """Use Claude to produce a semantically correct merge of two conflicting updates."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="""You are a JSON merge expert. Given a base object and two conflicting updates,
produce a merged result that preserves all new information from both updates.
Rules:
1. If both updates change the same field to different values, prefer the more specific/informative one.
2. If one update adds a new field, always include it.
3. Return ONLY valid JSON. No explanation.""",
        messages=[{
            "role": "user",
            "content": (
                f"Key: {key}\n"
                f"Base: {json.dumps(base)}\n"
                f"Update A: {json.dumps(version_a)}\n"
                f"Update B: {json.dumps(version_b)}\n"
                f"Merged result:"
            )
        }]
    )
    raw = response.content[0].text.strip()
    return json.loads(raw)


class MergingMemoryStore:
    def __init__(self):
        self._store: dict[str, tuple[dict, int]] = {}  # key → (value, version)
        self._lock = threading.Lock()

    def read(self, key: str) -> tuple[dict, int]:
        with self._lock:
            return self._store.get(key, ({}, 0))

    def write(self, key: str, new_value: dict, based_on_version: int, base_value: dict) -> dict:
        with self._lock:
            current_value, current_version = self._store.get(key, ({}, 0))

            if current_version == based_on_version:
                # No conflict — clean write
                self._store[key] = (new_value, current_version + 1)
                return new_value

            # Conflict detected — merge
            print(f"[merge] Conflict on '{key}' v{based_on_version} vs v{current_version} — invoking LLM merge")

        # LLM merge outside the lock (avoid holding lock during API call)
        merged = llm_merge(key, base_value, new_value, current_value)

        with self._lock:
            # Re-read after merge to get latest version
            _, latest_version = self._store.get(key, ({}, 0))
            self._store[key] = (merged, latest_version + 1)
            print(f"[merge] Merged result: {merged}")
            return merged


store = MergingMemoryStore()

def agent_update(agent_id: str, key: str, partial_update: dict) -> None:
    base, version = store.read(key)
    import time; time.sleep(0.005)  # Race window
    new_value = {**base, **partial_update}
    result = store.write(key, new_value, version, base)
    print(f"[{agent_id}] Final state: {result}")


# Seed initial state
store._store["profile"] = ({"name": "alice", "lang": "en", "theme": "dark"}, 1)

t1 = threading.Thread(target=agent_update, args=("Agent-A", "profile", {"theme": "light"}))
t2 = threading.Thread(target=agent_update, args=("Agent-B", "profile", {"lang": "fr", "timezone": "UTC+9"}))
t1.start(); t2.start()
t1.join(); t2.join()

# Expected Token Savings: LLM merge preserves all updates → no re-sending lost data in follow-up turns
# Environment: agents with rich structured memory (user profiles, task state, conversation context)
```

---

### Option 4 — CRDTs: conflict-free replicated data types for mergeable memory

Model memory as CRDTs where all concurrent writes can be merged without conflicts.

```python
import anthropic
import threading
import time
from copy import deepcopy

client = anthropic.Anthropic(api_key="sk-live-...")


class LWWRegister:
    """Last-Write-Wins Register: conflict-free for scalar values."""
    def __init__(self, value=None, timestamp: float = 0.0):
        self.value = value
        self.timestamp = timestamp

    def write(self, value, timestamp: float | None = None) -> "LWWRegister":
        ts = timestamp or time.monotonic()
        if ts >= self.timestamp:
            return LWWRegister(value, ts)
        return self  # Incoming write is older — keep current

    def merge(self, other: "LWWRegister") -> "LWWRegister":
        if other.timestamp >= self.timestamp:
            return LWWRegister(other.value, other.timestamp)
        return self


class GrowOnlySet:
    """Grow-Only Set: elements can only be added, never removed — always mergeable."""
    def __init__(self, items: set | None = None):
        self.items: set = items or set()

    def add(self, item) -> "GrowOnlySet":
        new = GrowOnlySet(self.items | {item})
        return new

    def merge(self, other: "GrowOnlySet") -> "GrowOnlySet":
        return GrowOnlySet(self.items | other.items)


class CRDTMemoryStore:
    """Memory store backed by CRDTs — all concurrent writes automatically merge."""

    def __init__(self):
        self._registers: dict[str, LWWRegister] = {}
        self._sets: dict[str, GrowOnlySet] = {}
        self._lock = threading.Lock()

    def set_value(self, key: str, value, timestamp: float | None = None) -> None:
        ts = timestamp or time.monotonic()
        with self._lock:
            reg = self._registers.get(key, LWWRegister())
            self._registers[key] = reg.write(value, ts)

    def get_value(self, key: str):
        with self._lock:
            reg = self._registers.get(key)
            return reg.value if reg else None

    def set_add(self, key: str, item) -> None:
        with self._lock:
            s = self._sets.get(key, GrowOnlySet())
            self._sets[key] = s.add(item)

    def set_get(self, key: str) -> set:
        with self._lock:
            return self._sets.get(key, GrowOnlySet()).items.copy()

    def merge_remote(self, remote_registers: dict, remote_sets: dict) -> None:
        """Merge state from a remote agent replica — always safe."""
        with self._lock:
            for key, remote_reg in remote_registers.items():
                local = self._registers.get(key, LWWRegister())
                self._registers[key] = local.merge(remote_reg)
            for key, remote_set in remote_sets.items():
                local = self._sets.get(key, GrowOnlySet())
                self._sets[key] = local.merge(remote_set)


# Two independent stores (simulating two agent replicas)
store_a = CRDTMemoryStore()
store_b = CRDTMemoryStore()

# Agent A writes at t=1.0
store_a.set_value("theme", "light", timestamp=1.0)
store_a.set_add("visited_pages", "/home")
store_a.set_add("visited_pages", "/settings")

# Agent B writes at t=1.1 (concurrent)
store_b.set_value("theme", "dark", timestamp=1.1)  # Later timestamp wins
store_b.set_add("visited_pages", "/checkout")
store_b.set_add("visited_pages", "/home")

# Merge — no conflicts possible
store_a.merge_remote(store_b._registers, store_b._sets)

print(f"Theme: {store_a.get_value('theme')}")         # → dark (higher timestamp)
print(f"Pages: {store_a.set_get('visited_pages')}")   # → {'/home', '/settings', '/checkout'}

# Expected Token Savings: CRDT merge is instantaneous and conflict-free → no LLM calls needed for resolution
# Environment: distributed agent clusters; edge agents syncing state to central store
```

---

### Option 5 — Redis-backed atomic compare-and-swap

Use Redis `WATCH` + `MULTI`/`EXEC` to implement atomic conditional updates in a distributed multi-agent system.

```python
import anthropic
import json
import threading
import time

client = anthropic.Anthropic(api_key="sk-live-...")

# Simulated Redis store (replace with redis.Redis() in production)
class SimulatedRedis:
    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()
        self._watches: dict[str, str | None] = {}

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def watch(self, key: str) -> str | None:
        """Watch a key; returns current value."""
        with self._lock:
            val = self._data.get(key)
            self._watches[key] = val
            return val

    def compare_and_set(self, key: str, expected: str | None, new_value: str) -> bool:
        """Set key=new_value only if current value == expected. Atomic."""
        with self._lock:
            current = self._data.get(key)
            if current != expected:
                return False
            self._data[key] = new_value
            return True


redis = SimulatedRedis()


def agent_update_redis(agent_id: str, key: str, updater) -> dict:
    """Read-modify-write with Redis CAS retry loop."""
    for attempt in range(10):
        raw = redis.watch(key)
        current = json.loads(raw) if raw else {}

        time.sleep(0.001)  # Simulate processing time (race window)

        new_value = updater(current)
        new_raw = json.dumps(new_value)

        if redis.compare_and_set(key, raw, new_raw):
            print(f"[{agent_id}] CAS succeeded (attempt {attempt + 1}): {new_value}")
            return new_value
        else:
            print(f"[{agent_id}] CAS failed (attempt {attempt + 1}) — retrying")

    raise RuntimeError(f"{agent_id}: CAS failed after 10 retries")


def agent_a() -> None:
    agent_update_redis("Agent-A", "memory:session", lambda c: {**c, "theme": "light"})


def agent_b() -> None:
    agent_update_redis("Agent-B", "memory:session", lambda c: {**c, "language": "ja", "tz": "Asia/Tokyo"})


# Seed initial value
redis._data["memory:session"] = json.dumps({"theme": "dark", "language": "en"})

t1 = threading.Thread(target=agent_a)
t2 = threading.Thread(target=agent_b)
t1.start(); t2.start()
t1.join(); t2.join()

final = json.loads(redis.get("memory:session") or "{}")
print(f"Final: {final}")
# Both agents' updates preserved

# Expected Token Savings: atomic CAS eliminates silent overwrites → no lost memory requiring re-explanation
# Environment: Redis-backed multi-agent deployments; horizontally scaled agent workers
```

---

### Option 6 — Event-sourced memory with immutable append log

Never overwrite memory — append events to an immutable log and reconstruct current state by replaying.

```python
import anthropic
import json
import threading
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class MemoryEvent:
    agent_id: str
    key: str
    operation: str   # "set", "delete", "merge"
    payload: dict
    timestamp: float = field(default_factory=time.monotonic)
    sequence: int = 0


class EventSourcedMemory:
    """Append-only event log. Conflicts visible in history; state is always reconstructable."""

    def __init__(self):
        self._log: list[MemoryEvent] = []
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, agent_id: str, key: str, operation: str, payload: dict) -> MemoryEvent:
        with self._lock:
            self._seq += 1
            event = MemoryEvent(agent_id, key, operation, payload, sequence=self._seq)
            self._log.append(event)
            return event

    def current_state(self, key: str) -> dict:
        """Reconstruct current state for a key by replaying events."""
        state = {}
        with self._lock:
            events = [e for e in self._log if e.key == key]
        for event in events:
            if event.operation == "set":
                state = dict(event.payload)
            elif event.operation == "merge":
                state.update(event.payload)
            elif event.operation == "delete":
                state.pop(event.payload.get("field", ""), None)
        return state

    def history(self, key: str) -> list[dict]:
        with self._lock:
            return [
                {
                    "seq": e.sequence,
                    "agent": e.agent_id,
                    "op": e.operation,
                    "payload": e.payload,
                    "ts": round(e.timestamp, 4)
                }
                for e in self._log if e.key == key
            ]

    def detect_conflicts(self, key: str, window: float = 0.01) -> list[list[MemoryEvent]]:
        """Find events that wrote to the same key within `window` seconds of each other."""
        with self._lock:
            events = sorted([e for e in self._log if e.key == key], key=lambda e: e.timestamp)
        conflicts = []
        i = 0
        while i < len(events):
            group = [events[i]]
            j = i + 1
            while j < len(events) and events[j].timestamp - events[i].timestamp < window:
                group.append(events[j])
                j += 1
            if len(group) > 1:
                conflicts.append(group)
            i = j if j > i + 1 else i + 1
        return conflicts


memory = EventSourcedMemory()


def agent_write_event(agent_id: str, key: str, updates: dict) -> None:
    # Read current state (non-atomic with write — that's OK, events capture all writes)
    _ = memory.current_state(key)
    time.sleep(0.002)  # Race window
    memory.append(agent_id, key, "merge", updates)
    print(f"[{agent_id}] Appended merge event: {updates}")


# Seed
memory.append("system", "prefs", "set", {"theme": "dark", "language": "en"})

t1 = threading.Thread(target=agent_write_event, args=("Agent-A", "prefs", {"theme": "light"}))
t2 = threading.Thread(target=agent_write_event, args=("Agent-B", "prefs", {"language": "ko"}))
t1.start(); t2.start()
t1.join(); t2.join()

print(f"\nCurrent state: {memory.current_state('prefs')}")
print("\nHistory:")
for entry in memory.history("prefs"):
    print(f"  #{entry['seq']} [{entry['agent']}] {entry['op']}: {entry['payload']}")

conflicts = memory.detect_conflicts("prefs")
if conflicts:
    print(f"\nConflicts detected: {len(conflicts)} group(s)")
    for group in conflicts:
        print(f"  Concurrent: {[f'{e.agent_id}:{e.operation}' for e in group]}")

# Expected Token Savings: full audit trail → conflicts visible without re-running agents; state always reconstructable
# Environment: agents with complex memory; systems requiring full auditability and replay
```

---

## Comparison

| Option | Prevents Data Loss | Detects Conflicts | Requires External Store | Merge Strategy | Complexity |
|--------|------------------|------------------|------------------------|---------------|------------|
| 1 | Yes | Yes | No | Retry | Low |
| 2 | LWW (partial) | Yes (audit log) | No | None | Low |
| 3 | Yes | Yes | No | LLM merge | Medium |
| 4 | Yes | N/A (CRDT) | No | CRDT | Medium |
| 5 | Yes | Yes | Redis | CAS retry | Medium |
| 6 | Yes | Yes (history) | No | Append/replay | Medium |

**Recommended starting point:** Option 1 (optimistic locking with retry) for single-process multi-threaded agents — it's a minimal wrapper around the existing store with zero external dependencies. Use Option 5 for Redis-backed distributed deployments. Use Option 6 when a full audit trail of memory changes is required for compliance or debugging.
