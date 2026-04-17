---
title: "Agent Doesn't Implement Agent Decision Explanation Logging"
description: "Agents that execute tool calls and route requests without logging the reasoning behind their choices produce opaque execution traces: engineers can see what the agent did but not why. Implement decision explanation logging that captures the agent's stated rationale for each significant choice — tool selection, routing decision, escalation trigger, or response strategy — alongside the inputs that drove that decision."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-decision-explanation-logging
tags: [decision-logging, explainability, agent-reasoning, audit-trail, tool-selection-trace, decision-rationale]
symptoms:
  - "Can see which tools were called but not why the agent chose them over alternatives"
  - "Unexpected agent behavior cannot be debugged because reasoning is not persisted"
  - "Compliance audits require showing why the agent took a specific action — no evidence available"
  - "Agent selects the wrong tool and there is no log of what criteria it used"
  - "Postmortems rely on replaying the full prompt rather than a structured decision record"
---

## Why This Happens

LLM-based agents often include chain-of-thought reasoning in their generation, but this reasoning lives only in the response text and is not extracted, structured, or persisted as a first-class observability artifact. When an agent selects a tool, routes to a sub-agent, or decides to escalate, the rationale for that decision — the inputs it considered and the criteria it applied — is discarded after the response is processed. Decision explanation logging requires extracting rationale from agent output, pairing it with the inputs that triggered the decision, and storing structured decision records that can be queried and analyzed independently of the full conversation trace.

## Solution 1: Decision Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DecisionKind(str, Enum):
    TOOL_SELECTION = "tool_selection"
    ROUTING = "routing"
    ESCALATION = "escalation"
    RESPONSE_STRATEGY = "response_strategy"
    RETRIEVAL_STRATEGY = "retrieval_strategy"
    FALLBACK = "fallback"
    ABSTENTION = "abstention"      # agent decided NOT to act


@dataclass
class AgentDecisionRecord:
    decision_id: str
    session_id: str
    turn_index: int
    kind: DecisionKind
    choice: str                    # the decision made (e.g. tool name, route name)
    alternatives_considered: List[str]   # other options the agent weighed
    rationale: str                 # extracted explanation from agent output
    confidence: float              # 0.0–1.0, if available
    input_summary: str             # brief summary of inputs driving the decision
    outcome: str = ""              # filled in after execution: "success" | "failure"
    outcome_detail: str = ""
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    recorded_at: float = field(default_factory=time.time)
```

## Solution 2: Rationale Extractor

```python
import re
from typing import List, Optional, Tuple


class AgentRationaleExtractor:
    """
    Extracts structured rationale from agent chain-of-thought output.
    Looks for explicit reasoning patterns and confidence signals.
    """

    RATIONALE_PATTERNS = [
        re.compile(r"(?:I (?:will|should|need to)|Let me|I'll)\s+(.{10,200}?)(?:\.|$)", re.IGNORECASE),
        re.compile(r"(?:Because|Since|Given that|Due to)\s+(.{10,200}?)(?:\.|$)", re.IGNORECASE),
        re.compile(r"(?:The best|The most appropriate|The right)\s+(?:choice|option|approach|tool)\s+(?:is|here is)\s+(.{5,100}?)(?:\.|$)", re.IGNORECASE),
        re.compile(r"(?:I chose|I selected|Using)\s+(.{5,100}?)\s+(?:because|since|as)\s+(.{10,200}?)(?:\.|$)", re.IGNORECASE),
    ]

    CONFIDENCE_HIGH = re.compile(r"\b(clearly|definitely|certainly|obviously|best)\b", re.IGNORECASE)
    CONFIDENCE_LOW = re.compile(r"\b(might|maybe|perhaps|possibly|uncertain|not sure)\b", re.IGNORECASE)

    def extract(self, agent_output: str) -> Tuple[str, float]:
        """
        Returns (rationale_text, confidence_score).
        """
        rationale_parts: List[str] = []
        for pattern in self.RATIONALE_PATTERNS:
            for match in pattern.finditer(agent_output):
                part = " ".join(g for g in match.groups() if g).strip()
                if part and len(part) > 10:
                    rationale_parts.append(part)

        rationale = " | ".join(dict.fromkeys(rationale_parts))[:500] if rationale_parts else agent_output[:200]

        # Confidence heuristic
        high_signals = len(self.CONFIDENCE_HIGH.findall(agent_output))
        low_signals = len(self.CONFIDENCE_LOW.findall(agent_output))
        confidence = 0.7 + min(high_signals * 0.05, 0.25) - min(low_signals * 0.1, 0.40)
        confidence = round(max(0.0, min(1.0, confidence)), 2)

        return rationale, confidence

    def extract_alternatives(self, agent_output: str) -> List[str]:
        """Extract tools or options the agent considered but did not choose."""
        pattern = re.compile(
            r"\b(?:instead of|rather than|not|avoid(?:ing)?)\s+([A-Za-z_]\w*(?:\s+\w+){0,3})",
            re.IGNORECASE,
        )
        return list(dict.fromkeys(m.group(1).strip() for m in pattern.finditer(agent_output)))[:5]
```

## Solution 3: Decision Logger

```python
import uuid
from typing import Dict, List, Optional


class AgentDecisionLogger:
    """
    Creates and stores AgentDecisionRecords for each significant agent decision.
    Provides structured lookup by session, kind, and outcome.
    """

    def __init__(
        self,
        extractor: AgentRationaleExtractor,
        max_records: int = 50000,
    ):
        self._extractor = extractor
        self._records: List[AgentDecisionRecord] = []
        self._max = max_records

    def log(
        self,
        session_id: str,
        turn_index: int,
        kind: DecisionKind,
        choice: str,
        agent_output: str,
        input_summary: str = "",
        alternatives_considered: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
    ) -> AgentDecisionRecord:
        rationale, confidence = self._extractor.extract(agent_output)
        alts = alternatives_considered or self._extractor.extract_alternatives(agent_output)

        record = AgentDecisionRecord(
            decision_id=str(uuid.uuid4())[:16],
            session_id=session_id,
            turn_index=turn_index,
            kind=kind,
            choice=choice,
            alternatives_considered=alts,
            rationale=rationale,
            confidence=confidence,
            input_summary=input_summary[:300],
            metadata=metadata or {},
        )

        if len(self._records) >= self._max:
            self._records = self._records[-self._max // 2:]
        self._records.append(record)
        return record

    def record_outcome(
        self,
        decision_id: str,
        outcome: str,
        detail: str = "",
        latency_ms: float = 0.0,
    ) -> None:
        for record in reversed(self._records):
            if record.decision_id == decision_id:
                record.outcome = outcome
                record.outcome_detail = detail
                record.latency_ms = latency_ms
                return

    def for_session(self, session_id: str) -> List[AgentDecisionRecord]:
        return [r for r in self._records if r.session_id == session_id]

    def by_kind(self, kind: DecisionKind) -> List[AgentDecisionRecord]:
        return [r for r in self._records if r.kind == kind]
```

## Solution 4: Decision Pattern Analyzer

```python
import time
from collections import defaultdict
from typing import Dict, List


class DecisionPatternAnalyzer:
    """
    Aggregates decision records to surface patterns:
    which choices fail most often, which alternatives are
    most frequently considered, and which decisions have
    low confidence but high failure rates.
    """

    def __init__(self, logger: AgentDecisionLogger):
        self._logger = logger

    def analyze(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._logger._records if r.recorded_at >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "decisions": 0}

        by_kind: Dict[str, int] = defaultdict(int)
        choice_outcomes: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        low_confidence_failures: List[dict] = []

        for r in recent:
            by_kind[r.kind.value] += 1
            if r.outcome:
                choice_outcomes[r.choice][r.outcome] += 1
            if r.confidence < 0.5 and r.outcome == "failure":
                low_confidence_failures.append({
                    "choice": r.choice,
                    "kind": r.kind.value,
                    "confidence": r.confidence,
                    "rationale": r.rationale[:100],
                })

        failure_rates = {
            choice: round(
                outcomes.get("failure", 0) / max(sum(outcomes.values()), 1), 3
            )
            for choice, outcomes in choice_outcomes.items()
        }

        return {
            "window_seconds": window_seconds,
            "decisions": len(recent),
            "by_kind": dict(by_kind),
            "choice_failure_rates": dict(sorted(failure_rates.items(), key=lambda x: -x[1])[:10]),
            "low_confidence_failures": low_confidence_failures[:10],
        }
```

## Solution 5: Decision Audit Exporter

```python
import json
import time
from typing import List, Optional


class DecisionAuditExporter:
    """
    Exports decision records for a session or time window as structured
    JSON for compliance reporting and postmortem analysis.
    """

    def __init__(self, logger: AgentDecisionLogger):
        self._logger = logger

    def export_session(self, session_id: str) -> str:
        records = self._logger.for_session(session_id)
        return json.dumps(
            {
                "session_id": session_id,
                "exported_at": time.time(),
                "decision_count": len(records),
                "decisions": [
                    {
                        "decision_id": r.decision_id,
                        "turn": r.turn_index,
                        "kind": r.kind.value,
                        "choice": r.choice,
                        "alternatives": r.alternatives_considered,
                        "rationale": r.rationale,
                        "confidence": r.confidence,
                        "input_summary": r.input_summary,
                        "outcome": r.outcome,
                        "outcome_detail": r.outcome_detail,
                        "latency_ms": r.latency_ms,
                        "recorded_at": r.recorded_at,
                    }
                    for r in records
                ],
            },
            indent=2,
        )
```

## Solution 6: Decision Explanation Dashboard

```python
import time


class DecisionExplanationDashboard:
    """
    Combines decision volume, pattern analysis, and recent low-confidence
    decisions into a single operational and quality view.
    """

    def __init__(
        self,
        logger: AgentDecisionLogger,
        analyzer: DecisionPatternAnalyzer,
    ):
        self._logger = logger
        self._analyzer = analyzer

    def render(self) -> dict:
        total = len(self._logger._records)
        recent = [r for r in self._logger._records if time.time() - r.recorded_at < 3600]
        return {
            "generated_at": time.time(),
            "lifetime_decisions": total,
            "last_hour_decisions": len(recent),
            "pattern_analysis": self._analyzer.analyze(window_seconds=3600.0),
            "recent_low_confidence": [
                {
                    "decision_id": r.decision_id,
                    "kind": r.kind.value,
                    "choice": r.choice,
                    "confidence": r.confidence,
                    "rationale": r.rationale[:120],
                }
                for r in sorted(recent, key=lambda r: r.confidence)[:5]
            ],
        }
```

## Comparison

| Approach | Rationale Extraction | Confidence Scoring | Outcome Tracking | Pattern Analysis | Audit Export |
|---|---|---|---|---|---|
| AgentRationaleExtractor | Yes (regex patterns) | Yes (heuristic) | No | No | No |
| AgentDecisionLogger | Via extractor | Via extractor | Yes | No | No |
| DecisionPatternAnalyzer | No | No | Via logger | Yes | No |
| DecisionAuditExporter | No | No | Via logger | No | Yes |
| DecisionExplanationDashboard | No | No | No | Via analyzer | No |

**Best for production**: Extract rationale immediately after every tool selection and routing decision — not at the end of the turn. The chain-of-thought tokens that justify a choice are present only in the generation that made the choice; subsequent turns may not reproduce the same reasoning. Log `alternatives_considered` even when the agent does not explicitly enumerate them — negative signals ("not using web search because…") are as valuable as positive ones. Use `DecisionPatternAnalyzer` to identify choices with both low confidence and high failure rates: these are the agent's known weak spots and should be addressed with better prompting or additional tools.
