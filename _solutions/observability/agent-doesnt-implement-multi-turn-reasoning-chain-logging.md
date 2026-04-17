---
title: "Agent Doesn't Implement Multi-Turn Reasoning Chain Logging"
description: "Agents that log only final answers omit the intermediate reasoning steps — tool selection rationale, hypothesis formation, evidence evaluation — that determine whether the final answer is reliable. Implement multi-turn reasoning chain logging that captures each reasoning step, links steps into a directed graph, and surfaces reasoning paths for quality review and regression detection."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-turn-reasoning-chain-logging
tags: [reasoning-chain, chain-of-thought, step-logging, reasoning-graph, intermediate-steps, quality-review]
symptoms:
  - "Cannot determine why the agent chose a particular tool sequence"
  - "Wrong final answers have no logged reasoning path to diagnose"
  - "Reasoning steps are printed to stdout during development but not persisted in production"
  - "No way to compare reasoning chains between a correct and incorrect run on the same question"
  - "Quality reviewers can only evaluate the final output, not the reasoning process"
---

## Why This Happens

LLM agents perform multi-step reasoning internally, but most logging systems only capture the final tool call and response. The intermediate steps — "I need to look up the user's account first, then check their subscription tier, then determine eligibility" — exist only in the LLM's context window and are lost after the session ends. Reasoning chain logging requires explicitly capturing each step as a structured event with a type, content, rationale, and link to preceding steps, forming a DAG that can be replayed and analyzed.

## Solution 1: Reasoning Step Model

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ReasoningStepType(str, Enum):
    HYPOTHESIS = "hypothesis"           # agent forms a hypothesis or plan
    TOOL_SELECTION = "tool_selection"   # agent decides which tool to call
    TOOL_EXECUTION = "tool_execution"   # tool call and result
    EVIDENCE_EVALUATION = "evidence_evaluation"  # agent evaluates tool result
    INTERMEDIATE_CONCLUSION = "intermediate_conclusion"
    FINAL_ANSWER = "final_answer"
    BACKTRACK = "backtrack"             # agent revises a prior decision


@dataclass
class ReasoningStep:
    step_id: str
    session_id: str
    step_type: ReasoningStepType
    content: str                        # the reasoning text or action
    timestamp: float = field(default_factory=time.time)
    parent_step_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    duration_ms: Optional[float] = None

    @staticmethod
    def create(
        session_id: str,
        step_type: ReasoningStepType,
        content: str,
        parent_step_ids: List[str] = None,
        **kwargs,
    ) -> "ReasoningStep":
        return ReasoningStep(
            step_id=str(uuid.uuid4())[:12],
            session_id=session_id,
            step_type=step_type,
            content=content,
            parent_step_ids=parent_step_ids or [],
            **kwargs,
        )
```

## Solution 2: Reasoning Chain Recorder

```python
import json
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class ReasoningChainRecorder:
    """
    Records reasoning steps per session and persists them to JSONL.
    Maintains an in-memory index for fast retrieval during the session.
    """

    def __init__(self, storage_dir: str = "/tmp/reasoning_chains"):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sessions: Dict[str, List[ReasoningStep]] = {}
        self._lock = Lock()

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}_reasoning.jsonl"

    def record(self, step: ReasoningStep) -> None:
        with self._lock:
            if step.session_id not in self._sessions:
                self._sessions[step.session_id] = []
            self._sessions[step.session_id].append(step)

        record = {
            "step_id": step.step_id,
            "session_id": step.session_id,
            "step_type": step.step_type.value,
            "content": step.content[:2000],
            "timestamp": step.timestamp,
            "parent_step_ids": step.parent_step_ids,
            "confidence": step.confidence,
            "duration_ms": step.duration_ms,
            "metadata": step.metadata,
        }
        with self._path(step.session_id).open("a") as f:
            f.write(json.dumps(record) + "\n")

    def get_chain(self, session_id: str) -> List[ReasoningStep]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def load_chain(self, session_id: str) -> List[ReasoningStep]:
        path = self._path(session_id)
        if not path.exists():
            return []
        steps = []
        for line in path.read_text().splitlines():
            try:
                data = json.loads(line)
                steps.append(ReasoningStep(
                    step_id=data["step_id"],
                    session_id=data["session_id"],
                    step_type=ReasoningStepType(data["step_type"]),
                    content=data["content"],
                    timestamp=data["timestamp"],
                    parent_step_ids=data.get("parent_step_ids", []),
                    confidence=data.get("confidence"),
                    duration_ms=data.get("duration_ms"),
                    metadata=data.get("metadata", {}),
                ))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return steps
```

## Solution 3: Reasoning Chain Builder

```python
import time
from typing import Optional


class ReasoningChainBuilder:
    """
    Fluent interface for building a reasoning chain within a session.
    Automatically links each new step to the previous one.
    """

    def __init__(self, session_id: str, recorder: ReasoningChainRecorder):
        self._session_id = session_id
        self._recorder = recorder
        self._last_step_id: Optional[str] = None
        self._step_count = 0

    def _record(self, step: ReasoningStep) -> ReasoningStep:
        self._recorder.record(step)
        self._last_step_id = step.step_id
        self._step_count += 1
        return step

    def hypothesis(self, content: str, confidence: Optional[float] = None) -> ReasoningStep:
        return self._record(ReasoningStep.create(
            self._session_id, ReasoningStepType.HYPOTHESIS, content,
            parent_step_ids=[self._last_step_id] if self._last_step_id else [],
            confidence=confidence,
        ))

    def tool_selection(self, tool_name: str, rationale: str) -> ReasoningStep:
        return self._record(ReasoningStep.create(
            self._session_id, ReasoningStepType.TOOL_SELECTION,
            f"Selected tool: {tool_name}. Rationale: {rationale}",
            parent_step_ids=[self._last_step_id] if self._last_step_id else [],
            metadata={"tool_name": tool_name},
        ))

    def tool_result(self, tool_name: str, result_summary: str, duration_ms: float) -> ReasoningStep:
        return self._record(ReasoningStep.create(
            self._session_id, ReasoningStepType.TOOL_EXECUTION,
            f"Tool '{tool_name}' returned: {result_summary[:500]}",
            parent_step_ids=[self._last_step_id] if self._last_step_id else [],
            duration_ms=duration_ms,
            metadata={"tool_name": tool_name},
        ))

    def evidence_evaluation(self, evaluation: str, confidence: Optional[float] = None) -> ReasoningStep:
        return self._record(ReasoningStep.create(
            self._session_id, ReasoningStepType.EVIDENCE_EVALUATION, evaluation,
            parent_step_ids=[self._last_step_id] if self._last_step_id else [],
            confidence=confidence,
        ))

    def final_answer(self, answer_summary: str, confidence: Optional[float] = None) -> ReasoningStep:
        return self._record(ReasoningStep.create(
            self._session_id, ReasoningStepType.FINAL_ANSWER, answer_summary,
            parent_step_ids=[self._last_step_id] if self._last_step_id else [],
            confidence=confidence,
        ))

    def backtrack(self, reason: str, revising_step_id: Optional[str] = None) -> ReasoningStep:
        parents = [self._last_step_id] if self._last_step_id else []
        if revising_step_id:
            parents.append(revising_step_id)
        return self._record(ReasoningStep.create(
            self._session_id, ReasoningStepType.BACKTRACK, reason,
            parent_step_ids=parents,
        ))

    def step_count(self) -> int:
        return self._step_count
```

## Solution 4: Reasoning Chain Analyzer

```python
from typing import Dict, List, Optional, Tuple


class ReasoningChainAnalyzer:
    """
    Analyzes a recorded reasoning chain for quality signals:
    depth, backtrack frequency, tool diversity, and confidence trajectory.
    """

    def analyze(self, steps: List[ReasoningStep]) -> dict:
        if not steps:
            return {"steps": 0}

        by_type: Dict[str, int] = {}
        for step in steps:
            t = step.step_type.value
            by_type[t] = by_type.get(t, 0) + 1

        tools_used = list({
            step.metadata.get("tool_name")
            for step in steps
            if step.metadata.get("tool_name")
        })

        backtracks = by_type.get("backtrack", 0)
        confidence_scores = [s.confidence for s in steps if s.confidence is not None]
        avg_confidence = (
            round(sum(confidence_scores) / len(confidence_scores), 3)
            if confidence_scores else None
        )

        tool_steps = [s for s in steps if s.step_type == ReasoningStepType.TOOL_EXECUTION]
        avg_tool_latency = (
            round(sum(s.duration_ms for s in tool_steps if s.duration_ms) /
                  max(len(tool_steps), 1), 2)
            if tool_steps else None
        )

        final = next((s for s in reversed(steps) if s.step_type == ReasoningStepType.FINAL_ANSWER), None)
        total_ms = (
            round((final.timestamp - steps[0].timestamp) * 1000, 2)
            if final else None
        )

        return {
            "total_steps": len(steps),
            "step_type_counts": by_type,
            "tools_used": tools_used,
            "backtrack_count": backtracks,
            "avg_confidence": avg_confidence,
            "avg_tool_latency_ms": avg_tool_latency,
            "total_reasoning_ms": total_ms,
            "reached_final_answer": final is not None,
        }
```

## Solution 5: Reasoning Chain Diff Engine

```python
from typing import List, Tuple


class ReasoningChainDiffEngine:
    """
    Compares two reasoning chains (e.g., correct vs. incorrect run on
    the same question) to find where the reasoning paths diverged.
    """

    def diff(
        self,
        chain_a: List[ReasoningStep],
        chain_b: List[ReasoningStep],
    ) -> List[dict]:
        diffs = []
        for i, (a, b) in enumerate(zip(chain_a, chain_b)):
            if a.step_type != b.step_type or a.content[:100] != b.content[:100]:
                diffs.append({
                    "position": i,
                    "chain_a": {"type": a.step_type.value, "content": a.content[:200]},
                    "chain_b": {"type": b.step_type.value, "content": b.content[:200]},
                })
        if len(chain_a) != len(chain_b):
            diffs.append({
                "position": "length_mismatch",
                "chain_a_steps": len(chain_a),
                "chain_b_steps": len(chain_b),
            })
        return diffs
```

## Solution 6: Reasoning Chain Dashboard

```python
import time
from typing import List


class ReasoningChainDashboard:
    """
    Operational view of reasoning chain health across recent sessions:
    average depth, backtrack rates, and confidence distributions.
    """

    def __init__(
        self,
        recorder: ReasoningChainRecorder,
        analyzer: ReasoningChainAnalyzer,
    ):
        self._recorder = recorder
        self._analyzer = analyzer

    def render(self, session_ids: List[str]) -> dict:
        analyses = []
        for sid in session_ids:
            steps = self._recorder.get_chain(sid)
            if steps:
                analyses.append(self._analyzer.analyze(steps))

        if not analyses:
            return {"sessions_analyzed": 0}

        avg_steps = sum(a["total_steps"] for a in analyses) / len(analyses)
        avg_backtracks = sum(a["backtrack_count"] for a in analyses) / len(analyses)
        reached_final = sum(1 for a in analyses if a["reached_final_answer"])

        return {
            "generated_at": time.time(),
            "sessions_analyzed": len(analyses),
            "avg_reasoning_steps": round(avg_steps, 1),
            "avg_backtracks": round(avg_backtracks, 2),
            "final_answer_rate": round(reached_final / len(analyses), 3),
            "high_backtrack_sessions": sum(1 for a in analyses if a["backtrack_count"] > 2),
        }
```

## Comparison

| Approach | Step Recording | Chain Linkage | Quality Analysis | Chain Diff | Dashboard |
|---|---|---|---|---|---|
| ReasoningChainRecorder | Yes (JSONL) | Via parent_step_ids | No | No | No |
| ReasoningChainBuilder | Via recorder | Yes (auto-link) | No | No | No |
| ReasoningChainAnalyzer | No | No | Yes | No | No |
| ReasoningChainDiffEngine | No | No | No | Yes | No |
| ReasoningChainDashboard | No | No | Via analyzer | No | Yes |

**Best for production**: Log reasoning steps at the granularity of "one step per LLM turn" — more granular logging (every sentence) is too noisy; coarser logging (just the final answer) loses diagnostic value. Use `ReasoningChainBuilder.backtrack()` whenever the agent explicitly revises a prior decision — high backtrack rates on a specific question type indicate the agent's initial tool selection strategy is miscalibrated for that domain. Store reasoning chains for at least 30 days for quality review; use `ReasoningChainDiffEngine` to compare correct and incorrect runs on identical questions during model evaluations — the divergence point is almost always the first tool selection, not the final synthesis.
