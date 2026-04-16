---
title: "Agent Doesn't Implement Automatic Schema Backfill for State Migrations"
description: "Agents that add new fields to their persistent state store without backfilling existing records create a split world: new sessions have the field, old sessions don't, and any code that reads the field without a default crashes on old records. Implement automatic schema backfill that detects missing fields on read, writes the default value back, and optionally runs a background migration pass over the full store."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-automatic-schema-backfill-for-state-migrations
tags: [schema-migration, backfill, state-evolution, forward-compatibility, lazy-migration, database-migration]
symptoms:
  - "KeyError or AttributeError on old records after adding a new field to the state schema"
  - "Agent crashes on sessions created before the last deployment that added a new field"
  - "Migration script fails halfway — half the records are on the new schema, half on the old"
  - "No record of which schema version each state entry was written with"
  - "Rollback impossible because the new schema's field values are already written to old records"
---

## Why This Happens

State schemas evolve: a field is added, renamed, or given a new type. The existing records in the store were written before the change and lack the new field. Without backfill, reading those records either returns `None` silently (masking bugs) or raises `KeyError` (crashing the agent). The safe pattern is three-part: (1) read-time lazy migration — detect the missing field on read and write the default; (2) a background batch pass — migrate all records in bulk at a controlled rate; (3) schema versioning — record which version wrote each entry to make forward and backward compatibility explicit.

## Solution 1: Schema Version and Field Descriptor

```python
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class FieldDescriptor:
    """Describes one field in the agent state schema."""
    name: str
    type: type
    default_factory: Callable[[], Any]
    introduced_in_version: int
    deprecated_in_version: Optional[int] = None
    migration_fn: Optional[Callable[[Any], Any]] = None   # transforms old value to new


@dataclass
class StateSchema:
    version: int
    fields: List[FieldDescriptor]

    def field_names(self) -> set:
        return {f.name for f in self.fields}

    def active_fields(self) -> List[FieldDescriptor]:
        return [
            f for f in self.fields
            if f.deprecated_in_version is None or f.deprecated_in_version > self.version
        ]
```

## Solution 2: Lazy Migration Reader

```python
from typing import Any, Dict, Optional, Tuple


class LazyMigrationReader:
    """
    Reads a state record and transparently fills in any missing fields
    using their schema defaults. Returns the record and a flag indicating
    whether any fields were backfilled (so the caller can persist the update).
    """

    def __init__(self, schema: StateSchema):
        self._schema = schema

    def read_and_backfill(
        self, record: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Returns (migrated_record, was_modified).
        Backfills missing fields and runs migration_fn on fields that have one.
        """
        modified = False
        result = dict(record)

        # Ensure schema_version is present
        if "__schema_version__" not in result:
            result["__schema_version__"] = 0
            modified = True

        record_version = result.get("__schema_version__", 0)

        for fd in self._schema.active_fields():
            # Field missing entirely
            if fd.name not in result:
                result[fd.name] = fd.default_factory()
                modified = True
                continue

            # Field present but schema has a migration function for it
            if (
                fd.migration_fn is not None
                and record_version < fd.introduced_in_version
            ):
                try:
                    result[fd.name] = fd.migration_fn(result[fd.name])
                    modified = True
                except Exception:
                    result[fd.name] = fd.default_factory()
                    modified = True

        if modified:
            result["__schema_version__"] = self._schema.version

        return result, modified
```

## Solution 3: Schema-Versioned State Store

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class SchemaVersionedStateStore:
    """
    State store wrapper that applies lazy migration on every read
    and persists backfilled records back to storage automatically.
    Exposes a migration pass for bulk background backfill.
    """

    def __init__(
        self,
        schema: StateSchema,
        reader: LazyMigrationReader,
        storage_backend: Dict[str, Dict[str, Any]],  # replace with real DB in prod
    ):
        self._schema = schema
        self._reader = reader
        self._storage = storage_backend
        self._backfill_count = 0
        self._read_count = 0

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self._storage.get(key)
        if raw is None:
            return None
        self._read_count += 1
        migrated, was_modified = self._reader.read_and_backfill(raw)
        if was_modified:
            self._storage[key] = migrated
            self._backfill_count += 1
        return migrated

    async def put(self, key: str, record: Dict[str, Any]) -> None:
        record["__schema_version__"] = self._schema.version
        record["__updated_at__"] = time.time()
        self._storage[key] = record

    async def delete(self, key: str) -> bool:
        return bool(self._storage.pop(key, None))

    async def run_migration_pass(
        self,
        batch_size: int = 100,
        delay_between_batches_seconds: float = 0.1,
        progress_fn: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """
        Iterates all records and backfills those on an older schema version.
        Runs in batches to avoid blocking other operations.
        """
        all_keys = list(self._storage.keys())
        total = len(all_keys)
        migrated = 0
        already_current = 0

        for i in range(0, total, batch_size):
            batch = all_keys[i: i + batch_size]
            for key in batch:
                raw = self._storage.get(key)
                if raw is None:
                    continue
                version = raw.get("__schema_version__", 0)
                if version >= self._schema.version:
                    already_current += 1
                    continue
                migrated_record, was_modified = self._reader.read_and_backfill(raw)
                if was_modified:
                    self._storage[key] = migrated_record
                    migrated += 1
                    self._backfill_count += 1

            if progress_fn:
                progress_fn(min(i + batch_size, total), total)
            if delay_between_batches_seconds > 0:
                await asyncio.sleep(delay_between_batches_seconds)

        return {
            "total_records": total,
            "migrated": migrated,
            "already_current": already_current,
            "target_version": self._schema.version,
        }

    def stats(self) -> dict:
        version_counts: Dict[int, int] = {}
        for rec in self._storage.values():
            v = rec.get("__schema_version__", 0)
            version_counts[v] = version_counts.get(v, 0) + 1
        return {
            "total_records": len(self._storage),
            "schema_version": self._schema.version,
            "version_distribution": version_counts,
            "lazy_backfills": self._backfill_count,
            "total_reads": self._read_count,
        }
```

## Solution 4: Migration Plan Builder

```python
from dataclasses import dataclass
from typing import List


@dataclass
class MigrationStep:
    from_version: int
    to_version: int
    description: str
    fields_added: List[str]
    fields_removed: List[str]
    fields_migrated: List[str]
    reversible: bool


class MigrationPlanBuilder:
    """
    Builds a documented migration plan from a sequence of schema versions.
    Used to communicate what changes each migration makes and whether
    it can be safely rolled back.
    """

    def __init__(self):
        self._steps: List[MigrationStep] = []

    def add_step(self, step: MigrationStep) -> "MigrationPlanBuilder":
        self._steps.append(step)
        return self

    def build(self) -> List[MigrationStep]:
        return sorted(self._steps, key=lambda s: s.from_version)

    def summary(self) -> dict:
        steps = self.build()
        irreversible = [s for s in steps if not s.reversible]
        return {
            "total_steps": len(steps),
            "irreversible_steps": len(irreversible),
            "steps": [
                {
                    "from": s.from_version,
                    "to": s.to_version,
                    "description": s.description,
                    "reversible": s.reversible,
                    "fields_added": s.fields_added,
                    "fields_removed": s.fields_removed,
                }
                for s in steps
            ],
        }
```

## Solution 5: Migration Guard

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MigrationRunRecord:
    migration_id: str
    from_version: int
    to_version: int
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    records_migrated: int = 0
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.completed_at is not None and self.error is None


class MigrationGuard:
    """
    Prevents duplicate migration runs and tracks migration history.
    Ensures a migration from version N to N+1 runs exactly once.
    """

    def __init__(self):
        self._history: List[MigrationRunRecord] = []
        self._running: Optional[str] = None

    def can_run(self, from_version: int, to_version: int) -> bool:
        for rec in self._history:
            if (
                rec.from_version == from_version
                and rec.to_version == to_version
                and rec.success
            ):
                return False   # already completed
        return True

    def start(self, migration_id: str, from_version: int, to_version: int) -> MigrationRunRecord:
        if self._running:
            raise RuntimeError(f"migration {self._running} is already running")
        rec = MigrationRunRecord(
            migration_id=migration_id,
            from_version=from_version,
            to_version=to_version,
        )
        self._history.append(rec)
        self._running = migration_id
        return rec

    def complete(self, rec: MigrationRunRecord, records_migrated: int) -> None:
        rec.completed_at = time.time()
        rec.records_migrated = records_migrated
        self._running = None

    def fail(self, rec: MigrationRunRecord, error: str) -> None:
        rec.error = error
        self._running = None

    def history(self) -> List[dict]:
        return [
            {
                "id": r.migration_id,
                "from": r.from_version,
                "to": r.to_version,
                "success": r.success,
                "records_migrated": r.records_migrated,
                "error": r.error,
            }
            for r in self._history
        ]
```

## Solution 6: Migration Health Monitor

```python
import time


class SchemaMigrationHealthMonitor:
    """
    Reports migration completeness and alerts when records on old schema versions
    exceed a threshold — indicating the migration pass has not been run.
    """

    def __init__(
        self,
        store: SchemaVersionedStateStore,
        guard: MigrationGuard,
        max_stale_ratio: float = 0.05,
    ):
        self._store = store
        self._guard = guard
        self._max_stale = max_stale_ratio

    def check(self) -> dict:
        stats = self._store.stats()
        current_version = stats["schema_version"]
        dist = stats["version_distribution"]
        total = stats["total_records"]

        stale = sum(
            count for ver, count in dist.items() if ver < current_version
        )
        stale_ratio = stale / max(total, 1)

        alerts = []
        if stale_ratio > self._max_stale:
            alerts.append({
                "type": "stale_records",
                "stale_count": stale,
                "stale_ratio": round(stale_ratio, 4),
                "threshold": self._max_stale,
                "recommendation": "run run_migration_pass() to backfill old records",
            })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "schema_version": current_version,
            "version_distribution": dist,
            "stale_records": stale,
            "stale_ratio": round(stale_ratio, 4),
            "migration_history": self._guard.history()[-5:],
            "alerts": alerts,
        }
```

## Comparison

| Approach | Lazy Backfill | Bulk Migration | Version Tracking | Rollback Safety |
|---|---|---|---|---|
| LazyMigrationReader | Yes (on read) | No | Via field version | Via reversible flag |
| SchemaVersionedStateStore | Yes | Yes (batch pass) | Yes (__schema_version__) | No |
| MigrationPlanBuilder | No | No | Yes | Yes (reversible) |
| MigrationGuard | No | No | No | Yes (idempotent) |
| SchemaMigrationHealthMonitor | No | No | Via store | No (alerts only) |

**Best for production**: Always default new fields in `FieldDescriptor.default_factory` to a safe sentinel (`None`, `[]`, `0`) — never to a computed value that might differ between lazy and bulk migration. Run the bulk pass during a low-traffic window after deployment, using `delay_between_batches_seconds=0.1` to rate-limit disk/DB writes. Use `MigrationGuard` to ensure the bulk pass is idempotent — safe to re-run if it fails partway through. Monitor `SchemaMigrationHealthMonitor.check()` post-deployment; a stale ratio above 5% means the bulk pass hasn't run and lazy migration is bearing the full load.
