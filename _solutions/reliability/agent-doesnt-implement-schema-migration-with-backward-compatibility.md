---
title: "Agent Doesn't Implement Schema Migration with Backward Compatibility"
description: "AI agents that change their state schemas or API contracts without backward-compatible migrations cause data corruption, deserialization crashes, and rolling-deployment failures. Learn six patterns for zero-downtime schema evolution."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-schema-migration-with-backward-compatibility
tags: [schema, migration, backward-compatibility, versioning, deployment, reliability]
symptoms:
  - "Agent crashes on startup after deployment because it can't deserialize old state"
  - "Rolling deployment leaves some instances reading v1 and others writing v2 state"
  - "Tool call payloads break silently when a field is renamed or removed"
  - "Conversation history becomes unreadable after a prompt format change"
  - "Rollback to previous version causes data loss or startup failure"
---

## The Problem

AI agents persist state — conversation history, tool results, user preferences, session metadata — in databases, Redis, or files. When the schema of that state changes (a field is added, renamed, or removed), agents deployed in a rolling fashion face a window where both old and new code runs simultaneously. Old instances write v1 records; new instances expect v2. Without explicit migration strategies, this causes deserialization crashes, silent data loss, or invalid state that corrupts subsequent reasoning.

```python
# ❌ Silently breaks on old data
@dataclass
class SessionState:
    user_id: str
    context: list[dict]        # was: conversation_history
    last_model: str             # new field — crashes on old pickled records

state = pickle.loads(redis.get(session_id))  # KeyError / AttributeError

# ✓ Schema-versioned with migration chain
state = VersionedStateStore.load(session_id)  # auto-migrates v1→v2→v3
```

---

## Solution 1: Versioned Schema with Explicit Migration Chain

Embed a `schema_version` field in every persisted record. On load, detect the version and apply migrations in sequence until the current version is reached.

```python
from dataclasses import dataclass, field, asdict
from typing import Any
import json


CURRENT_VERSION = 3


@dataclass
class SessionState:
    schema_version: int
    user_id: str
    context: list[dict]
    last_model: str
    metadata: dict = field(default_factory=dict)


# --- Migration functions (v_from → v_from+1) ---

def migrate_v1_to_v2(data: dict) -> dict:
    """v1 stored conversation_history; v2 renamed to context."""
    data = dict(data)
    if "conversation_history" in data:
        data["context"] = data.pop("conversation_history")
    data["schema_version"] = 2
    return data


def migrate_v2_to_v3(data: dict) -> dict:
    """v3 added last_model field with a default."""
    data = dict(data)
    data.setdefault("last_model", "claude-3-5-sonnet-20241022")
    data.setdefault("metadata", {})
    data["schema_version"] = 3
    return data


MIGRATIONS = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
}


class VersionedStateStore:
    def __init__(self, backend):  # backend: any key-value store
        self.backend = backend

    def save(self, key: str, state: SessionState):
        data = asdict(state)
        data["schema_version"] = CURRENT_VERSION
        self.backend.set(key, json.dumps(data))

    def load(self, key: str) -> SessionState | None:
        raw = self.backend.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        data = self._migrate(data)
        return SessionState(**{k: data[k] for k in SessionState.__dataclass_fields__})

    def _migrate(self, data: dict) -> dict:
        version = data.get("schema_version", 1)
        while version < CURRENT_VERSION:
            if version not in MIGRATIONS:
                raise ValueError(f"No migration from v{version} to v{version + 1}")
            data = MIGRATIONS[version](data)
            version = data["schema_version"]
        return data

    def migrate_all(self, key_pattern: str):
        """Offline migration: upgrade all records to current version."""
        for key in self.backend.scan(key_pattern):
            raw = self.backend.get(key)
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("schema_version", 1) < CURRENT_VERSION:
                migrated = self._migrate(data)
                self.backend.set(key, json.dumps(migrated))
                print(f"Migrated {key}: v{data.get('schema_version', 1)} → v{CURRENT_VERSION}")
```

---

## Solution 2: Expand-Contract Pattern for Rolling Deployments

During rolling deployments, both old and new instances run simultaneously. Use the expand-contract (parallel change) pattern: first expand the schema to support both old and new shapes, then contract once all instances are upgraded.

```python
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional


# Phase 1: EXPAND — new code writes both old and new fields
class SessionStateExpand(BaseModel):
    """Written during the expand phase. Both field names present."""
    schema_version: int = 2

    # Old field name (kept for old readers)
    conversation_history: Optional[list[dict]] = None

    # New field name (written by new code)
    context: Optional[list[dict]] = None

    last_model: str = "claude-3-5-sonnet-20241022"

    @model_validator(mode="before")
    @classmethod
    def sync_old_and_new(cls, values):
        """Keep both fields in sync so old and new code can read."""
        old = values.get("conversation_history")
        new = values.get("context")
        if new is not None and old is None:
            values["conversation_history"] = new
        elif old is not None and new is None:
            values["context"] = old
        return values

    def effective_context(self) -> list[dict]:
        return self.context or self.conversation_history or []


# Phase 2: CONTRACT — old field removed once all instances upgraded
class SessionStateContract(BaseModel):
    """Written during the contract phase. Old field dropped."""
    schema_version: int = 3
    context: list[dict] = []
    last_model: str = "claude-3-5-sonnet-20241022"
    metadata: dict = {}


class ExpandContractStore:
    """
    Detects deployment phase and reads/writes accordingly.
    Phase is controlled by a feature flag.
    """

    def __init__(self, backend, feature_flags):
        self.backend = backend
        self.flags = feature_flags

    def save(self, key: str, context: list[dict], last_model: str):
        if self.flags.get("schema_v3_contract"):
            record = SessionStateContract(context=context, last_model=last_model)
        else:
            # Expand phase: write both fields
            record = SessionStateExpand(context=context, last_model=last_model)
        self.backend.set(key, record.model_dump_json())

    def load(self, key: str) -> list[dict]:
        raw = self.backend.get(key)
        if not raw:
            return []
        data = __import__("json").loads(raw)
        version = data.get("schema_version", 1)
        if version <= 2:
            # Can be read by both expand and old code
            parsed = SessionStateExpand(**data)
            return parsed.effective_context()
        return data.get("context", [])
```

---

## Solution 3: Pydantic Schema with Auto-Coercion and Default Injection

For agents using Pydantic for state validation, use field aliases, validators, and `model_validate` with `strict=False` to absorb old-format payloads without explicit migration code.

```python
from pydantic import BaseModel, Field, field_validator, AliasChoices
from typing import Any
import json


class ToolResult(BaseModel):
    """Tool result schema that handles v1 (flat) and v2 (nested) formats."""
    tool_name: str = Field(validation_alias=AliasChoices("tool_name", "name", "tool"))
    output: Any = Field(validation_alias=AliasChoices("output", "result", "content"))
    duration_ms: float = Field(default=0.0,
                               validation_alias=AliasChoices("duration_ms", "elapsed_ms", "ms"))
    error: str | None = Field(default=None,
                              validation_alias=AliasChoices("error", "error_message", "err"))

    model_config = {"populate_by_name": True}


class ConversationTurn(BaseModel):
    """Handles schema changes across agent versions."""
    role: str
    content: str | list[dict]  # v1: str, v2: list of content blocks
    tool_results: list[ToolResult] = []
    turn_id: str | None = None
    timestamp_ms: int | None = None

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, v):
        """v1 stored plain strings; v2 uses content blocks."""
        if isinstance(v, str):
            return [{"type": "text", "text": v}]
        return v

    @field_validator("tool_results", mode="before")
    @classmethod
    def normalize_tool_results(cls, v):
        """v1 stored tool results as plain dicts."""
        if not v:
            return []
        return [ToolResult.model_validate(r) if isinstance(r, dict) else r for r in v]

    def text_content(self) -> str:
        """Extract plain text regardless of schema version."""
        if isinstance(self.content, str):
            return self.content
        return " ".join(
            block.get("text", "") for block in self.content
            if isinstance(block, dict) and block.get("type") == "text"
        )


class SchemaCoercingStore:
    def __init__(self, backend):
        self.backend = backend

    def save_turn(self, session_id: str, turn: ConversationTurn):
        key = f"session:{session_id}:turns"
        self.backend.rpush(key, turn.model_dump_json())

    def load_turns(self, session_id: str) -> list[ConversationTurn]:
        key = f"session:{session_id}:turns"
        raw_list = self.backend.lrange(key, 0, -1) or []
        turns = []
        for raw in raw_list:
            try:
                data = json.loads(raw)
                turns.append(ConversationTurn.model_validate(data))
            except Exception as e:
                print(f"[warn] Skipping unreadable turn: {e}")
        return turns
```

---

## Solution 4: Event-Sourced State with Schema-Versioned Events

Instead of storing current state, store immutable events. Each event carries its schema version. State is reconstructed by replaying events through version-aware reducers.

```python
from dataclasses import dataclass, field
from typing import Any
import json
import time
import uuid


@dataclass
class Event:
    event_type: str
    schema_version: int
    payload: dict
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class AgentState:
    context: list[dict] = field(default_factory=list)
    model: str = "claude-3-5-sonnet-20241022"
    tool_budget_remaining: int = 20
    metadata: dict = field(default_factory=dict)


# --- Event reducers by type and schema version ---

def reduce_turn_added(state: AgentState, event: Event) -> AgentState:
    payload = event.payload
    if event.schema_version == 1:
        # v1: stored role+content as flat strings
        message = {"role": payload["role"], "content": payload["content"]}
    elif event.schema_version >= 2:
        # v2+: content is a list of blocks
        message = {"role": payload["role"], "content": payload.get("content_blocks", [
            {"type": "text", "text": payload.get("content", "")}
        ])}
    else:
        return state

    return AgentState(
        context=state.context + [message],
        model=state.model,
        tool_budget_remaining=state.tool_budget_remaining,
        metadata=state.metadata,
    )


def reduce_model_changed(state: AgentState, event: Event) -> AgentState:
    payload = event.payload
    model = payload.get("model") or payload.get("model_id") or state.model
    return AgentState(
        context=state.context,
        model=model,
        tool_budget_remaining=state.tool_budget_remaining,
        metadata=state.metadata,
    )


REDUCERS = {
    "turn_added": reduce_turn_added,
    "model_changed": reduce_model_changed,
}


class EventSourcedAgentStore:
    def __init__(self, event_log):
        self.log = event_log

    def append(self, session_id: str, event: Event):
        self.log.append(session_id, json.dumps({
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "payload": event.payload,
            "event_id": event.event_id,
            "timestamp_ms": event.timestamp_ms,
        }))

    def replay(self, session_id: str) -> AgentState:
        state = AgentState()
        for raw in self.log.read(session_id):
            data = json.loads(raw)
            event = Event(**data)
            reducer = REDUCERS.get(event.event_type)
            if reducer:
                state = reducer(state, event)
            else:
                print(f"[warn] Unknown event type: {event.event_type}, skipping")
        return state

    def add_turn(self, session_id: str, role: str, content: str | list):
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        self.append(session_id, Event(
            event_type="turn_added",
            schema_version=2,
            payload={"role": role, "content_blocks": blocks},
        ))
```

---

## Solution 5: Blue-Green Schema Dual-Write with Cutover Toggle

For high-traffic agents, use dual-write: every write goes to both the old schema store and the new schema store simultaneously. Reads come from the old store until a cutover flag is flipped. Rollback is instant.

```python
import asyncio
from typing import Any
import json


class DualWriteSchemaStore:
    """
    Writes to both v1 and v2 stores simultaneously.
    Reads from whichever is designated by the cutover flag.
    Rollback = flip the flag back.
    """

    def __init__(self, v1_store, v2_store, feature_flags):
        self.v1 = v1_store
        self.v2 = v2_store
        self.flags = feature_flags

    def _to_v1(self, state: dict) -> dict:
        """Downconvert v2 state to v1 format."""
        v1 = dict(state)
        # v2 uses 'context', v1 uses 'conversation_history'
        if "context" in v1:
            v1["conversation_history"] = v1.pop("context")
        # v1 doesn't have metadata
        v1.pop("metadata", None)
        v1.pop("schema_version", None)
        return v1

    def _to_v2(self, state: dict) -> dict:
        """Upconvert v1 state to v2 format."""
        v2 = dict(state)
        if "conversation_history" in v2:
            v2["context"] = v2.pop("conversation_history")
        v2.setdefault("metadata", {})
        v2["schema_version"] = 2
        return v2

    async def save(self, key: str, state: dict):
        v2_state = self._to_v2(state)
        v1_state = self._to_v1(v2_state)

        # Write to both — if v2 write fails, v1 is still good
        results = await asyncio.gather(
            self._safe_write(self.v1, key, v1_state),
            self._safe_write(self.v2, key, v2_state),
            return_exceptions=True,
        )
        if isinstance(results[0], Exception):
            raise RuntimeError(f"v1 write failed: {results[0]}")
        if isinstance(results[1], Exception):
            print(f"[warn] v2 write failed (non-fatal during dual-write): {results[1]}")

    async def _safe_write(self, store, key: str, data: dict):
        await store.set(key, json.dumps(data))

    async def load(self, key: str) -> dict | None:
        use_v2 = self.flags.get("schema_v2_read")
        store = self.v2 if use_v2 else self.v1
        raw = await store.get(key)
        if not raw:
            # Fallback to the other store
            fallback = self.v1 if use_v2 else self.v2
            raw = await fallback.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        return self._to_v2(data)

    async def verify_consistency(self, key: str) -> bool:
        """Check that both stores agree on critical fields."""
        v1_raw = await self.v1.get(key)
        v2_raw = await self.v2.get(key)
        if not v1_raw or not v2_raw:
            return v1_raw == v2_raw  # both None is OK
        v1 = json.loads(v1_raw)
        v2 = json.loads(v2_raw)
        v1_ctx = v1.get("conversation_history", [])
        v2_ctx = v2.get("context", [])
        return len(v1_ctx) == len(v2_ctx)
```

---

## Solution 6: Schema Registry with Compatibility Enforcement

A central schema registry validates every write against a registered schema and enforces compatibility rules (BACKWARD, FORWARD, FULL) before accepting schema updates.

```python
from enum import Enum
from pydantic import BaseModel
from typing import Any
import json


class CompatibilityMode(Enum):
    BACKWARD = "backward"   # New schema can read old data
    FORWARD = "forward"     # Old schema can read new data
    FULL = "full"           # Both directions
    NONE = "none"           # No compatibility check


class SchemaSpec(BaseModel):
    subject: str            # e.g., "session-state"
    version: int
    schema_json: dict
    compatibility: CompatibilityMode = CompatibilityMode.BACKWARD


class SchemaRegistryClient:
    """Validates schema evolution before writes are accepted."""

    def __init__(self):
        self._registry: dict[str, list[SchemaSpec]] = {}

    def register(self, spec: SchemaSpec) -> bool:
        subject = spec.subject
        existing = self._registry.get(subject, [])

        if existing:
            latest = existing[-1]
            ok, reason = self._check_compatibility(latest, spec)
            if not ok:
                raise ValueError(
                    f"Schema v{spec.version} for '{subject}' violates "
                    f"{spec.compatibility.value} compatibility: {reason}"
                )

        self._registry.setdefault(subject, []).append(spec)
        return True

    def _check_compatibility(self, old: SchemaSpec, new: SchemaSpec) -> tuple[bool, str]:
        old_fields = set(old.schema_json.get("properties", {}).keys())
        new_fields = set(new.schema_json.get("properties", {}).keys())
        old_required = set(old.schema_json.get("required", []))
        new_required = set(new.schema_json.get("required", []))

        mode = new.compatibility

        if mode in (CompatibilityMode.BACKWARD, CompatibilityMode.FULL):
            # New schema must be able to read old data:
            # Cannot add new required fields (old data won't have them)
            new_required_fields = new_required - old_required
            if new_required_fields:
                return False, f"Added required fields without defaults: {new_required_fields}"
            # Cannot remove optional fields that old data might have
            # (fine — unknown fields are ignored)

        if mode in (CompatibilityMode.FORWARD, CompatibilityMode.FULL):
            # Old schema must be able to read new data:
            # Cannot remove fields that old schema requires
            removed_required = old_required - new_fields
            if removed_required:
                return False, f"Removed fields required by old schema: {removed_required}"

        return True, "ok"

    def validate_payload(self, subject: str, version: int, payload: dict) -> bool:
        specs = self._registry.get(subject, [])
        spec = next((s for s in specs if s.version == version), None)
        if not spec:
            raise ValueError(f"Unknown schema {subject} v{version}")
        required = set(spec.schema_json.get("required", []))
        missing = required - set(payload.keys())
        if missing:
            raise ValueError(f"Payload missing required fields: {missing}")
        return True

    def latest_version(self, subject: str) -> int | None:
        specs = self._registry.get(subject, [])
        return specs[-1].version if specs else None


# Usage example
registry = SchemaRegistryClient()

registry.register(SchemaSpec(
    subject="session-state", version=1,
    schema_json={
        "properties": {"user_id": {}, "conversation_history": {}, "model": {}},
        "required": ["user_id", "conversation_history"],
    }
))

# This will PASS: adds optional field, no new required fields
registry.register(SchemaSpec(
    subject="session-state", version=2,
    schema_json={
        "properties": {"user_id": {}, "context": {}, "model": {}, "metadata": {}},
        "required": ["user_id", "context"],
    },
    compatibility=CompatibilityMode.BACKWARD,
))

# This would FAIL: "context" is required in v2 but removed in v3
# registry.register(SchemaSpec(subject="session-state", version=3,
#     schema_json={"properties": {"user_id": {}}, "required": ["user_id"]}))
```

---

## Comparison

| Pattern | Zero-Downtime | Rollback Safety | Complexity | Best For |
|---|---|---|---|---|
| Versioned migration chain | Yes (lazy migrate on read) | Manual offline migration | Low | Small-scale agents with simple state |
| Expand-contract | Yes (dual-field window) | Yes (flip flag) | Medium | Rolling deployments with shared Redis |
| Pydantic auto-coercion | Yes (reads any version) | N/A (read-side only) | Low | Tool result / API payload normalization |
| Event sourcing | Yes (replay any version) | Yes (replay old events) | High | Agents needing full audit trail |
| Blue-green dual-write | Yes (instant cutover) | Yes (flag flip) | High | High-traffic, zero tolerance for data loss |
| Schema registry | Yes (enforced at deploy) | Blocked at registration | Medium | Teams deploying frequently with multiple services |

**Recommendations:**
- Use **versioned migration chain** (Solution 1) for any agent that persists state — it costs almost nothing and prevents the hardest-to-debug crashes.
- Use **expand-contract** (Solution 2) during rolling deployments where old and new instances overlap.
- Use **Pydantic auto-coercion** (Solution 3) for tool call payloads and API response parsing where you don't control the producer.
- Use **event sourcing** (Solution 4) when you need full replay capability for debugging and audit.
- Use **dual-write** (Solution 5) when you need instant rollback without data loss at high write volume.
- Use the **schema registry** (Solution 6) in multi-team environments to catch incompatible changes before they reach production.
