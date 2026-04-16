---
title: "Agent Doesn't Implement Task Assignment with Capability Matching"
description: "Multi-agent systems that assign tasks round-robin or to any available worker ignore declared agent capabilities, current load, and skill affinity — causing tasks to land on agents that lack the tools, model access, or domain context to complete them. Implement capability-based task routing that matches tasks to agents by required skills, current load, and historical success rate."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-task-assignment-with-capability-matching
tags: [capability-matching, task-routing, multi-agent, load-balancing, skill-affinity, reliability]
symptoms:
  - "Code-review task routed to an agent without code execution tools — fails immediately"
  - "All tasks go to the first available agent regardless of specialization"
  - "Agent with 20 queued tasks receives new work while another agent is idle"
  - "No tracking of which agents succeed at which task types"
  - "Task requiring GPT-4 access routed to agent configured with Claude-only credentials"
---

## Why This Happens

Round-robin and random assignment treat all agents as identical. But in production multi-agent systems, agents differ by available tools, model access, memory capacity, current workload, and historical success rates per task type. Capability matching routes each task to the agent best positioned to complete it — not just any idle agent. This reduces failure rates, improves throughput, and allows specialization to emerge naturally from success-rate tracking.

## Solution 1: Agent Capability Registry

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

@dataclass
class AgentCapability:
    skill_id: str           # "code_execution" | "web_search" | "sql_query" | ...
    proficiency: float      # 0.0–1.0, self-reported or measured
    max_concurrent: int = 1 # how many tasks of this type simultaneously

@dataclass
class AgentProfile:
    agent_id: str
    name: str
    capabilities: List[AgentCapability] = field(default_factory=list)
    available_models: List[str] = field(default_factory=list)
    max_queue_depth: int = 10
    tags: Set[str] = field(default_factory=set)   # "gpu", "high-memory", "eu-region"

    def has_skill(self, skill_id: str) -> bool:
        return any(c.skill_id == skill_id for c in self.capabilities)

    def proficiency(self, skill_id: str) -> float:
        for c in self.capabilities:
            if c.skill_id == skill_id:
                return c.proficiency
        return 0.0

    def supports_model(self, model_id: str) -> bool:
        if not self.available_models:
            return True   # unrestricted
        return any(model_id in m or m in model_id for m in self.available_models)

class AgentCapabilityRegistry:
    def __init__(self):
        self._agents: Dict[str, AgentProfile] = {}

    def register(self, profile: AgentProfile) -> None:
        self._agents[profile.agent_id] = profile

    def deregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def find_capable(
        self,
        required_skills: List[str],
        required_model: Optional[str] = None,
        required_tags: Optional[Set[str]] = None,
    ) -> List[AgentProfile]:
        results = []
        for profile in self._agents.values():
            if not all(profile.has_skill(s) for s in required_skills):
                continue
            if required_model and not profile.supports_model(required_model):
                continue
            if required_tags and not required_tags.issubset(profile.tags):
                continue
            results.append(profile)
        return results

    def all_agents(self) -> List[AgentProfile]:
        return list(self._agents.values())
```

## Solution 2: Task Requirement Specification

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

@dataclass
class TaskRequirements:
    task_id: str
    task_type: str          # semantic category for success-rate lookup
    required_skills: List[str] = field(default_factory=list)
    preferred_skills: List[str] = field(default_factory=list)
    required_model: Optional[str] = None
    required_tags: Set[str] = field(default_factory=set)
    estimated_tokens: int = 1000
    deadline: Optional[float] = None   # unix timestamp
    payload: Any = None
    priority: int = 1       # 1 = normal, 0 = high, 2 = low

    @property
    def is_expired(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline

    @property
    def urgency_score(self) -> float:
        if self.deadline is None:
            return 0.0
        remaining = self.deadline - time.time()
        if remaining <= 0:
            return 1.0
        return max(0.0, 1.0 - remaining / 3600.0)
```

## Solution 3: Capability-Aware Task Scorer

```python
import time
from typing import Dict, List, Optional, Tuple

class CapabilityAwareTaskScorer:
    """
    Scores each (agent, task) pair on: capability match, workload,
    historical success rate, and affinity (how often this agent type
    succeeds at this task type).
    Returns ranked list of (agent_id, score) for assignment.
    """

    def __init__(
        self,
        registry: AgentCapabilityRegistry,
        load_tracker: "AgentLoadTracker",
        success_tracker: "AgentSuccessTracker",
    ):
        self._registry = registry
        self._load = load_tracker
        self._success = success_tracker

    def score_agent(self, profile: AgentProfile, task: TaskRequirements) -> float:
        # Capability match: required skills
        if not all(profile.has_skill(s) for s in task.required_skills):
            return 0.0   # hard requirement not met
        if task.required_model and not profile.supports_model(task.required_model):
            return 0.0
        if task.required_tags and not task.required_tags.issubset(profile.tags):
            return 0.0

        # Base score from required skill proficiency
        req_proficiency = 1.0
        for skill in task.required_skills:
            req_proficiency *= profile.proficiency(skill)

        # Bonus for preferred skills
        pref_bonus = sum(
            profile.proficiency(s) * 0.1
            for s in task.preferred_skills
            if profile.has_skill(s)
        )

        # Load penalty: penalize agents with deep queues
        load_ratio = self._load.load_ratio(profile.agent_id)
        load_score = 1.0 - load_ratio   # 0.0 = fully loaded, 1.0 = idle

        # Historical success rate for this task type
        success_rate = self._success.success_rate(profile.agent_id, task.task_type)

        # Composite score
        score = (
            req_proficiency * 0.35
            + pref_bonus
            + load_score * 0.30
            + success_rate * 0.35
        )
        return round(score, 4)

    def rank_agents(
        self,
        task: TaskRequirements,
        top_k: int = 5,
    ) -> List[Tuple[AgentProfile, float]]:
        candidates = self._registry.find_capable(
            task.required_skills,
            task.required_model,
            task.required_tags if task.required_tags else None,
        )
        scored = [
            (profile, self.score_agent(profile, task))
            for profile in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(p, s) for p, s in scored[:top_k] if s > 0]
```

## Solution 4: Agent Load Tracker

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass
class AgentLoadState:
    agent_id: str
    active_tasks: int = 0
    queued_tasks: int = 0
    last_heartbeat: float = field(default_factory=time.time)
    max_queue_depth: int = 10

    @property
    def total_load(self) -> int:
        return self.active_tasks + self.queued_tasks

    @property
    def is_overloaded(self) -> bool:
        return self.total_load >= self.max_queue_depth

    @property
    def is_stale(self) -> bool:
        return time.time() - self.last_heartbeat > 30.0

class AgentLoadTracker:
    def __init__(self):
        self._states: Dict[str, AgentLoadState] = {}

    def update(self, agent_id: str, active: int, queued: int, max_depth: int = 10) -> None:
        if agent_id not in self._states:
            self._states[agent_id] = AgentLoadState(agent_id=agent_id, max_queue_depth=max_depth)
        s = self._states[agent_id]
        s.active_tasks = active
        s.queued_tasks = queued
        s.max_queue_depth = max_depth
        s.last_heartbeat = time.time()

    def task_assigned(self, agent_id: str) -> None:
        if agent_id in self._states:
            self._states[agent_id].queued_tasks += 1

    def task_started(self, agent_id: str) -> None:
        if agent_id in self._states:
            s = self._states[agent_id]
            s.queued_tasks = max(0, s.queued_tasks - 1)
            s.active_tasks += 1

    def task_completed(self, agent_id: str) -> None:
        if agent_id in self._states:
            self._states[agent_id].active_tasks = max(0, self._states[agent_id].active_tasks - 1)

    def load_ratio(self, agent_id: str) -> float:
        state = self._states.get(agent_id)
        if not state:
            return 0.0
        return state.total_load / max(state.max_queue_depth, 1)

    def available_agents(self) -> List[str]:
        return [
            aid for aid, s in self._states.items()
            if not s.is_overloaded and not s.is_stale
        ]
```

## Solution 5: Agent Success Tracker

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

@dataclass
class TaskOutcome:
    agent_id: str
    task_type: str
    success: bool
    duration_ms: float
    timestamp: float

class AgentSuccessTracker:
    """
    Tracks per-agent, per-task-type success rates using a sliding window.
    Used by the scorer to prefer agents with proven success at a task type.
    """

    def __init__(self, window_size: int = 100):
        self._window = window_size
        # (agent_id, task_type) -> deque of (success: bool, timestamp)
        self._outcomes: Dict[Tuple[str, str], Deque] = defaultdict(
            lambda: deque(maxlen=window_size)
        )

    def record(self, outcome: TaskOutcome) -> None:
        key = (outcome.agent_id, outcome.task_type)
        self._outcomes[key].append((outcome.success, outcome.timestamp))

    def success_rate(self, agent_id: str, task_type: str) -> float:
        key = (agent_id, task_type)
        outcomes = self._outcomes.get(key)
        if not outcomes or len(outcomes) < 3:
            return 0.7   # optimistic prior for new agent/task combos
        successes = sum(1 for ok, _ in outcomes if ok)
        return successes / len(outcomes)

    def agent_summary(self, agent_id: str) -> dict:
        result = {}
        for (aid, task_type), outcomes in self._outcomes.items():
            if aid != agent_id:
                continue
            successes = sum(1 for ok, _ in outcomes if ok)
            result[task_type] = {
                "success_rate": round(successes / max(len(outcomes), 1), 3),
                "sample_size": len(outcomes),
            }
        return result
```

## Solution 6: Capability-Matched Task Dispatcher

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class DispatchResult:
    task_id: str
    assigned_agent_id: Optional[str]
    score: float
    reason: str
    dispatch_latency_ms: float

class CapabilityMatchedDispatcher:
    """
    Combines registry, scorer, and load tracker into a single dispatch call.
    Falls back to least-loaded capable agent if no high-score match found.
    """

    def __init__(
        self,
        scorer: CapabilityAwareTaskScorer,
        load_tracker: AgentLoadTracker,
        min_score_threshold: float = 0.2,
    ):
        self._scorer = scorer
        self._load = load_tracker
        self._threshold = min_score_threshold
        self._dispatched = 0
        self._no_match_count = 0

    async def dispatch(
        self,
        task: TaskRequirements,
        assign_fn: Callable[[str, TaskRequirements], asyncio.Coroutine],
    ) -> DispatchResult:
        t0 = time.monotonic()

        if task.is_expired:
            return DispatchResult(
                task_id=task.task_id,
                assigned_agent_id=None,
                score=0.0,
                reason="task_expired",
                dispatch_latency_ms=0.0,
            )

        ranked = self._scorer.rank_agents(task)

        if not ranked:
            self._no_match_count += 1
            return DispatchResult(
                task_id=task.task_id,
                assigned_agent_id=None,
                score=0.0,
                reason="no_capable_agents",
                dispatch_latency_ms=(time.monotonic() - t0) * 1000,
            )

        best_agent, best_score = ranked[0]

        if best_score < self._threshold:
            self._no_match_count += 1
            return DispatchResult(
                task_id=task.task_id,
                assigned_agent_id=None,
                score=best_score,
                reason="score_below_threshold",
                dispatch_latency_ms=(time.monotonic() - t0) * 1000,
            )

        self._load.task_assigned(best_agent.agent_id)
        await assign_fn(best_agent.agent_id, task)
        self._dispatched += 1

        return DispatchResult(
            task_id=task.task_id,
            assigned_agent_id=best_agent.agent_id,
            score=best_score,
            reason="capability_match",
            dispatch_latency_ms=round((time.monotonic() - t0) * 1000, 2),
        )

    def dispatch_stats(self) -> dict:
        return {
            "total_dispatched": self._dispatched,
            "no_match_count": self._no_match_count,
            "match_rate": round(
                self._dispatched / max(self._dispatched + self._no_match_count, 1), 3
            ),
        }
```

## Comparison

| Approach | Skill Matching | Load Balancing | Success Feedback | Deadline Aware |
|---|---|---|---|---|
| AgentCapabilityRegistry | Yes (hard + soft) | No | No | No |
| CapabilityAwareTaskScorer | Yes | Via load tracker | Yes | No |
| AgentLoadTracker | No | Yes (ratio) | No | No |
| AgentSuccessTracker | No | No | Yes (sliding window) | No |
| CapabilityMatchedDispatcher | Via scorer | Via load tracker | Via scorer | Yes (expiry) |

**Best for production**: Register all agents at startup via `AgentCapabilityRegistry` with declared skills and proficiency scores. Use `CapabilityMatchedDispatcher` for every task assignment — it combines skill match, load balance, and historical success in one score. Run `AgentSuccessTracker` continuously so scores reflect real-world performance rather than declared capability. Set `min_score_threshold` conservatively and route unmatched tasks to a fallback queue rather than a random agent.
