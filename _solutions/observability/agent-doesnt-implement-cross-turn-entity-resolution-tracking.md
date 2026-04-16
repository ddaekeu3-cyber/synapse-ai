---
title: "Agent Doesn't Implement Cross-Turn Entity Resolution Tracking"
description: "Agents that handle multi-turn conversations resolve user-referenced entities (a user mentioning 'the report from last week' or 'that customer') on each turn independently without tracking whether the resolution was consistent across turns. Inconsistent entity resolution — where 'the file' refers to different files in consecutive turns — causes confused reasoning without any observable signal. Implement cross-turn entity resolution tracking that records entity references, their resolutions, and detects inconsistencies across the conversation."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-cross-turn-entity-resolution-tracking
tags: [entity-resolution, cross-turn, coreference, entity-tracking, conversation-coherence, reference-disambiguation]
symptoms:
  - "Agent refers to a different file than the user intended in turn 4 vs turn 2"
  - "Entity references like 'that customer' resolve to different IDs across turns"
  - "No log of which entity was resolved to which identifier on each turn"
  - "Inconsistent tool calls caused by entity resolution drift in long conversations"
  - "Cannot diagnose whether a wrong answer was caused by entity confusion"
---

## Why This Happens

LLMs resolve entity references contextually based on the conversation history present in the current prompt. In long conversations, the full history may be truncated or pruned, causing earlier entity resolutions to fall out of context. The agent re-resolves the reference without the previous context and may reach a different conclusion. Without an explicit entity registry that stores resolved identifiers across turns, there is no mechanism to detect or correct this drift.

## Solution 1: Entity Reference

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EntityType(str, Enum):
    FILE = "file"
    CUSTOMER = "customer"
    ORDER = "order"
    USER = "user"
    DOCUMENT = "document"
    QUERY = "query"
    GENERIC = "generic"


@dataclass
class EntityReference:
    mention: str              # how the user referred to the entity ("the report")
    entity_type: EntityType
    resolved_id: Optional[str]   # canonical identifier (file path, customer ID, etc.)
    resolved_value: Any = None    # full resolved object or summary
    turn_index: int = 0
    confidence: float = 1.0
    resolved_at: float = field(default_factory=time.time)
    tool_name: Optional[str] = None   # which tool produced the resolution
```

## Solution 2: Entity Registry

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional


class EntityRegistry:
    """
    Stores entity resolutions across turns for a single session.
    Provides lookup by mention text and entity type.
    """

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._entities: Dict[str, List[EntityReference]] = {}
        # key: (mention_normalized, entity_type)
        self._lock = Lock()

    @staticmethod
    def _key(mention: str, entity_type: EntityType) -> str:
        return f"{entity_type.value}:{mention.lower().strip()}"

    def register(self, ref: EntityReference) -> None:
        key = self._key(ref.mention, ref.entity_type)
        with self._lock:
            if key not in self._entities:
                self._entities[key] = []
            self._entities[key].append(ref)

    def lookup(
        self,
        mention: str,
        entity_type: EntityType,
    ) -> Optional[EntityReference]:
        """Returns the most recent resolution for this mention."""
        key = self._key(mention, entity_type)
        with self._lock:
            refs = self._entities.get(key, [])
        return refs[-1] if refs else None

    def all_resolutions(self, mention: str, entity_type: EntityType) -> List[EntityReference]:
        key = self._key(mention, entity_type)
        with self._lock:
            return list(self._entities.get(key, []))

    def all_entities(self) -> List[EntityReference]:
        with self._lock:
            return [ref for refs in self._entities.values() for ref in refs]
```

## Solution 3: Resolution Inconsistency Detector

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ResolutionInconsistency:
    mention: str
    entity_type: EntityType
    turn_a: int
    resolved_id_a: Optional[str]
    turn_b: int
    resolved_id_b: Optional[str]
    description: str


class ResolutionInconsistencyDetector:
    """
    Scans the entity registry for mentions that have been resolved
    to different identifiers on different turns.
    """

    def detect(self, registry: EntityRegistry) -> List[ResolutionInconsistency]:
        inconsistencies = []

        with registry._lock:
            all_refs = {k: list(v) for k, v in registry._entities.items()}

        for key, refs in all_refs.items():
            if len(refs) < 2:
                continue

            # Check for divergent resolved_ids across turns
            ids_seen = {}
            for ref in refs:
                rid = ref.resolved_id
                if rid is not None:
                    if rid not in ids_seen:
                        ids_seen[rid] = ref
                    else:
                        prev = ids_seen[rid]
                        # Check for an inconsistent resolution
                        for other_ref in refs:
                            if (other_ref.resolved_id != rid
                                    and other_ref.resolved_id is not None
                                    and other_ref.turn_index > prev.turn_index):
                                inconsistencies.append(ResolutionInconsistency(
                                    mention=ref.mention,
                                    entity_type=ref.entity_type,
                                    turn_a=prev.turn_index,
                                    resolved_id_a=rid,
                                    turn_b=other_ref.turn_index,
                                    resolved_id_b=other_ref.resolved_id,
                                    description=(
                                        f"'{ref.mention}' resolved to '{rid}' on turn {prev.turn_index} "
                                        f"but '{other_ref.resolved_id}' on turn {other_ref.turn_index}"
                                    ),
                                ))
                        break

        return inconsistencies
```

## Solution 4: Entity Extraction Instrumentor

```python
import re
from typing import Any, Dict, List, Optional


_ENTITY_MENTION_PATTERNS = [
    (re.compile(r"\b(the file|that file|the document|that document)\b", re.I), EntityType.FILE),
    (re.compile(r"\b(the customer|that customer|the client|that client)\b", re.I), EntityType.CUSTOMER),
    (re.compile(r"\b(the order|that order|the request)\b", re.I), EntityType.ORDER),
    (re.compile(r"\b(the user|that user)\b", re.I), EntityType.USER),
    (re.compile(r"\b(the report|that report|the result)\b", re.I), EntityType.DOCUMENT),
]


class EntityExtractionInstrumentor:
    """
    Extracts implicit entity references from user messages and marks
    them for resolution tracking. Populates the registry with mentions
    even before they are resolved.
    """

    def extract_mentions(
        self,
        text: str,
        turn_index: int,
    ) -> List[EntityReference]:
        refs = []
        for pattern, entity_type in _ENTITY_MENTION_PATTERNS:
            for match in pattern.finditer(text):
                refs.append(EntityReference(
                    mention=match.group(0),
                    entity_type=entity_type,
                    resolved_id=None,    # not yet resolved
                    turn_index=turn_index,
                    confidence=0.70,
                ))
        return refs

    def record_resolution(
        self,
        registry: EntityRegistry,
        mention: str,
        entity_type: EntityType,
        resolved_id: str,
        turn_index: int,
        tool_name: Optional[str] = None,
    ) -> EntityReference:
        ref = EntityReference(
            mention=mention,
            entity_type=entity_type,
            resolved_id=resolved_id,
            turn_index=turn_index,
            confidence=0.95,
            tool_name=tool_name,
        )
        registry.register(ref)
        return ref
```

## Solution 5: Entity Coherence Monitor

```python
import time
from typing import List


class EntityCoherenceMonitor:
    """
    Monitors session entity registries for inconsistencies and
    emits structured coherence reports for debugging.
    """

    def __init__(self, detector: ResolutionInconsistencyDetector):
        self._detector = detector
        self._reports: List[dict] = []

    def evaluate(self, registry: EntityRegistry, session_id: str) -> dict:
        inconsistencies = self._detector.detect(registry)
        all_entities = registry.all_entities()

        report = {
            "ts": time.time(),
            "session_id": session_id,
            "entity_references": len(all_entities),
            "unique_mentions": len(set(r.mention.lower() for r in all_entities)),
            "inconsistencies": len(inconsistencies),
            "inconsistency_details": [
                {
                    "mention": inc.mention,
                    "entity_type": inc.entity_type.value,
                    "description": inc.description,
                }
                for inc in inconsistencies
            ],
            "coherent": len(inconsistencies) == 0,
        }
        self._reports.append(report)
        return report

    def fleet_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._reports if r["ts"] >= cutoff]
        if not recent:
            return {"sessions": 0}
        incoherent = sum(1 for r in recent if not r["coherent"])
        return {
            "sessions": len(recent),
            "incoherent_sessions": incoherent,
            "incoherence_rate": round(incoherent / len(recent), 4),
        }
```

## Solution 6: Entity Resolution Dashboard

```python
import time
from typing import Optional


class CrossTurnEntityResolutionDashboard:
    """
    Renders entity registry state, inconsistency detections,
    and fleet coherence rates for session debugging.
    """

    def __init__(
        self,
        monitor: EntityCoherenceMonitor,
        detector: ResolutionInconsistencyDetector,
    ):
        self._monitor = monitor
        self._detector = detector

    def render_session(self, registry: EntityRegistry, session_id: str) -> dict:
        report = self._monitor.evaluate(registry, session_id)
        return {
            "generated_at": time.time(),
            "session_report": report,
            "fleet_1h": self._monitor.fleet_summary(3600.0),
        }
```

## Comparison

| Approach | Entity Tracking | Cross-Turn Lookup | Inconsistency Detection | Mention Extraction | Fleet Monitoring |
|---|---|---|---|---|---|
| EntityRegistry | Yes (per session) | Yes | No | No | No |
| ResolutionInconsistencyDetector | Via registry | Via registry | Yes | No | No |
| EntityExtractionInstrumentor | No | No | No | Yes | No |
| EntityCoherenceMonitor | Via detector | No | Via detector | No | Yes |
| CrossTurnEntityResolutionDashboard | No | No | No | No | Via monitor |

**Best for production**: Record entity resolutions whenever a tool call produces a specific identifier — if `search_files` returns `/data/report.pdf` in response to a query about "the report", register that resolution immediately. This creates the audit trail needed to detect drift. Alert on sessions with inconsistencies above 2 — one inconsistency may be user intent change, but multiple indicate reasoning confusion. Use `EntityRegistry.lookup()` at the start of each turn to inject a "previously resolved entities" section into the prompt, reinforcing consistent resolution across turns even when history is truncated.
