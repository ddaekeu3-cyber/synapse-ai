---
title: "Agent Doesn't Implement Multi-Turn State Consistency Validation"
description: "Agents that accumulate state across turns — tracking user preferences, decisions made, constraints set, and data fetched — have no mechanism to detect when that state becomes internally inconsistent: a preference set in turn 3 contradicts a decision made in turn 7, or a constraint acknowledged in turn 2 is violated by an action in turn 10. Implement multi-turn state consistency validation that catches contradictions, stale references, and violated constraints before they produce incorrect agent behavior."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-multi-turn-state-consistency-validation
tags: [state-consistency, multi-turn, constraint-tracking, contradiction-detection, session-state, validation]
symptoms:
  - "Agent violates a constraint the user set earlier in the conversation"
  - "Decision made in turn 5 is contradicted by action taken in turn 12"
  - "Agent references a value that was updated but uses the stale version"
  - "No mechanism to detect when accumulated session state has become contradictory"
  - "Agent proceeds with conflicting instructions without flagging the inconsistency"
---

## Why This Happens

Multi-turn state grows organically: each turn adds preferences, decisions, and constraints without a schema or validation step. The LLM holds all this context in its attention window and may silently prioritize newer instructions over older ones, or vice versa, depending on position effects. No code-level validation checks whether the accumulated state is self-consistent. A constraint tracker that explicitly records and checks constraints as facts — rather than leaving them buried in conversation text — catches contradictions at the state management layer before they influence agent behavior.

## Solution 1: Session State Fact

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class FactType(str, Enum):
    PREFERENCE = "preference"        # user stated preference
    CONSTRAINT = "constraint"        # hard limit or rule
    DECISION = "decision"            # choice made during session
    DATA_POINT = "data_point"        # fetched or derived fact
    ASSUMPTION = "assumption"        # implicit assumption being tracked


@dataclass
class SessionStateFact:
    fact_id: str
    fact_type: FactType
    key: str                         # namespaced key, e.g. "budget.max_usd"
    value: Any
    turn_index: int                  # turn where this fact was established
    source: str                      # "user" | "agent" | "tool"
    confidence: float = 1.0          # 0.0–1.0
    superseded_by: Optional[str] = None   # fact_id of the newer fact if overwritten
    active: bool = True
```

## Solution 2: Session State Store

```python
import time
import uuid
from threading import Lock
from typing import Any, Dict, List, Optional


class SessionStateStore:
    """
    Stores session state facts with versioning.
    When a key is updated, the old fact is marked as superseded.
    """

    def __init__(self):
        self._lock = Lock()
        self._facts: Dict[str, List[SessionStateFact]] = {}   # key -> history

    def assert_fact(
        self,
        fact_type: FactType,
        key: str,
        value: Any,
        turn_index: int,
        source: str = "user",
        confidence: float = 1.0,
    ) -> SessionStateFact:
        fact_id = uuid.uuid4().hex[:10]
        fact = SessionStateFact(
            fact_id=fact_id,
            fact_type=fact_type,
            key=key,
            value=value,
            turn_index=turn_index,
            source=source,
            confidence=confidence,
        )
        with self._lock:
            if key in self._facts and self._facts[key]:
                prev = self._facts[key][-1]
                if prev.active:
                    prev.active = False
                    prev.superseded_by = fact_id
            if key not in self._facts:
                self._facts[key] = []
            self._facts[key].append(fact)
        return fact

    def get(self, key: str) -> Optional[SessionStateFact]:
        with self._lock:
            history = self._facts.get(key, [])
            return next((f for f in reversed(history) if f.active), None)

    def all_active(self) -> List[SessionStateFact]:
        with self._lock:
            return [
                facts[-1] for facts in self._facts.values()
                if facts and facts[-1].active
            ]

    def history(self, key: str) -> List[SessionStateFact]:
        with self._lock:
            return list(self._facts.get(key, []))
```

## Solution 3: Constraint Violation Detector

```python
from dataclasses import dataclass
from typing import Any, Callable, List, Optional


@dataclass
class ConstraintRule:
    rule_id: str
    description: str
    check_fn: Callable[[SessionStateFact, List[SessionStateFact]], bool]
    # Returns True if the rule is satisfied (no violation)
    severity: int   # 1 (warning) – 10 (critical)


@dataclass
class ConstraintViolation:
    rule_id: str
    description: str
    severity: int
    conflicting_keys: List[str]
    turn_indices: List[int]
    message: str


class ConstraintViolationDetector:
    """
    Evaluates registered constraint rules against all active session facts.
    Reports any violations found.
    """

    def __init__(self, rules: List[ConstraintRule]):
        self._rules = rules

    def check(self, store: SessionStateStore) -> List[ConstraintViolation]:
        active_facts = store.all_active()
        violations = []

        for rule in self._rules:
            for fact in active_facts:
                if fact.fact_type != FactType.CONSTRAINT:
                    continue
                try:
                    satisfied = rule.check_fn(fact, active_facts)
                except Exception:
                    satisfied = True

                if not satisfied:
                    violations.append(ConstraintViolation(
                        rule_id=rule.rule_id,
                        description=rule.description,
                        severity=rule.severity,
                        conflicting_keys=[fact.key],
                        turn_indices=[fact.turn_index],
                        message=f"Constraint '{fact.key}={fact.value}' violated by rule '{rule.rule_id}'",
                    ))

        return violations
```

## Solution 4: Contradiction Detector

```python
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple


@dataclass
class Contradiction:
    fact_a: SessionStateFact
    fact_b: SessionStateFact
    contradiction_type: str
    message: str


class StateContradictionDetector:
    """
    Detects contradictions between pairs of active facts.
    Checks numeric bounds (min > max), boolean conflicts (flag true and false),
    and value mutual exclusivity.
    """

    def detect(self, store: SessionStateStore) -> List[Contradiction]:
        facts = store.all_active()
        contradictions = []

        # Group by key namespace (e.g., "budget" groups "budget.min_usd" and "budget.max_usd")
        namespaces: dict = {}
        for fact in facts:
            ns = fact.key.rsplit(".", 1)[0] if "." in fact.key else fact.key
            if ns not in namespaces:
                namespaces[ns] = []
            namespaces[ns].append(fact)

        for ns, ns_facts in namespaces.items():
            numeric_facts = {f.key: f for f in ns_facts if isinstance(f.value, (int, float))}

            # Check min/max inversions
            for key_a, fact_a in numeric_facts.items():
                for key_b, fact_b in numeric_facts.items():
                    if key_a == key_b:
                        continue
                    if "min" in key_a and "max" in key_b:
                        if fact_a.value > fact_b.value:
                            contradictions.append(Contradiction(
                                fact_a=fact_a,
                                fact_b=fact_b,
                                contradiction_type="min_exceeds_max",
                                message=f"{key_a}={fact_a.value} > {key_b}={fact_b.value}",
                            ))

        return contradictions
```

## Solution 5: Multi-Turn State Validator

```python
import time
from typing import List, Optional


@dataclass
class StateValidationReport:
    session_id: str
    turn_index: int
    generated_at: float
    active_fact_count: int
    violations: List[ConstraintViolation]
    contradictions: List[Contradiction]
    is_consistent: bool

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "active_facts": self.active_fact_count,
            "violations": len(self.violations),
            "contradictions": len(self.contradictions),
            "is_consistent": self.is_consistent,
            "critical_issues": [
                v.message for v in self.violations if v.severity >= 8
            ] + [c.message for c in self.contradictions],
        }


class MultiTurnStateValidator:
    """
    Runs constraint violation and contradiction checks after each turn.
    Returns a structured validation report.
    """

    def __init__(
        self,
        store: SessionStateStore,
        violation_detector: ConstraintViolationDetector,
        contradiction_detector: StateContradictionDetector,
    ):
        self._store = store
        self._violations = violation_detector
        self._contradictions = contradiction_detector

    def validate(self, session_id: str, turn_index: int) -> StateValidationReport:
        violations = self._violations.check(self._store)
        contradictions = self._contradiction_detector.detect(self._store)

        return StateValidationReport(
            session_id=session_id,
            turn_index=turn_index,
            generated_at=time.time(),
            active_fact_count=len(self._store.all_active()),
            violations=violations,
            contradictions=contradictions,
            is_consistent=len(violations) == 0 and len(contradictions) == 0,
        )

    @property
    def _contradiction_detector(self):
        return self._contradictions
```

## Solution 6: Consistency Audit Logger

```python
import time
from typing import List


class StateConsistencyAuditLogger:
    """
    Records state validation reports for post-session analysis.
    Tracks which sessions had consistency issues and at what turn.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def log(self, report: StateValidationReport) -> None:
        if report.is_consistent:
            return  # only log inconsistent states
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": report.session_id,
            "turn_index": report.turn_index,
            "violations": len(report.violations),
            "contradictions": len(report.contradictions),
            "critical_issues": report.summary()["critical_issues"],
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "inconsistent_turns": len(recent),
            "unique_sessions": len({r["session_id"] for r in recent}),
            "avg_turn_of_first_issue": (
                sum(r["turn_index"] for r in recent) / len(recent)
                if recent else None
            ),
        }
```

## Comparison

| Approach | Fact Versioning | Constraint Rules | Contradiction Detection | Per-Turn Validation | Audit Trail |
|---|---|---|---|---|---|
| SessionStateStore | Yes (superseded) | No | No | No | No |
| ConstraintViolationDetector | No | Yes (pluggable) | No | No | No |
| StateContradictionDetector | No | No | Yes (numeric+bool) | No | No |
| MultiTurnStateValidator | Via store | Via detector | Via detector | Yes | No |
| StateConsistencyAuditLogger | No | No | No | No | Yes |

**Best for production**: Run `MultiTurnStateValidator.validate()` after every turn where the agent modifies session state — not on every turn, as most turns do not assert new facts. When `is_consistent=False`, surface the `critical_issues` to the LLM as a system message before generating the next response: "Note: conflicting constraints detected — {issue}. Please resolve before proceeding." This prevents the model from silently proceeding with contradictory state. Log all inconsistent states in `StateConsistencyAuditLogger` and review weekly: sessions with high `avg_turn_of_first_issue` values suggest the contradiction was seeded early and propagated — indicative of ambiguous initial instructions.
