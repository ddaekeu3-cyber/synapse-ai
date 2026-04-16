---
title: "Agent Doesn't Implement Cross-Session User Behavior Pattern Analysis"
description: "Agents that observe only single-session interactions miss longitudinal patterns: users who always ask the same question, cohorts that churn after specific failure types, or power-user workflows spanning dozens of sessions. Implement cross-session behavior analysis to surface patterns that inform personalization, onboarding, and product decisions."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cross-session-user-behavior-pattern-analysis
tags: [user-behavior, cross-session, analytics, observability, cohort-analysis, pattern-detection]
symptoms:
  - "No way to know which users ask the same question repeatedly across sessions"
  - "Cannot identify users who consistently hit the same tool failure before churning"
  - "No cohort-level analysis: do Pro users behave differently than Free users?"
  - "User journey analytics show single-session funnels but not multi-session arcs"
  - "Product team asks 'what do power users do differently?' and there is no data"
---

## Why This Happens

Session-level analytics treat each conversation as independent. Logs, metrics, and traces are scoped to a single session ID. Cross-session analysis requires joining data by user ID across sessions, computing rolling aggregates, detecting sequential patterns, and clustering users by behavioral similarity. Without a dedicated cross-session store and analysis pipeline, these questions can only be answered with expensive ad-hoc queries.

## Solution 1: User Session Aggregator

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class SessionSummary:
    session_id: str
    user_id: str
    started_at: float
    ended_at: Optional[float]
    turn_count: int
    tool_calls: List[str]       # tool names used
    intents: List[str]          # classified user intents
    outcome: str                # completed | abandoned | error
    error_types: List[str]
    tokens_used: int

@dataclass
class UserProfile:
    user_id: str
    session_count: int = 0
    total_turns: int = 0
    total_tokens: int = 0
    frequent_tools: Dict[str, int] = field(default_factory=dict)
    frequent_intents: Dict[str, int] = field(default_factory=dict)
    error_history: List[str] = field(default_factory=list)
    last_seen: float = 0.0
    first_seen: float = 0.0
    retention_days: float = 0.0
    sessions: List[str] = field(default_factory=list)

class UserSessionAggregator:
    """
    Maintains a rolling profile for each user by aggregating session summaries.
    Updates incrementally as sessions complete; does not require full recompute.
    """

    def __init__(self, profile_store):
        self._store = profile_store

    async def ingest_session(self, summary: SessionSummary) -> UserProfile:
        profile = await self._store.get(summary.user_id) or UserProfile(
            user_id=summary.user_id,
            first_seen=summary.started_at,
        )

        profile.session_count += 1
        profile.total_turns += summary.turn_count
        profile.total_tokens += summary.tokens_used
        profile.last_seen = summary.started_at
        profile.sessions.append(summary.session_id)

        if profile.first_seen > 0:
            profile.retention_days = (profile.last_seen - profile.first_seen) / 86400

        for tool in summary.tool_calls:
            profile.frequent_tools[tool] = profile.frequent_tools.get(tool, 0) + 1

        for intent in summary.intents:
            profile.frequent_intents[intent] = profile.frequent_intents.get(intent, 0) + 1

        profile.error_history.extend(summary.error_types)
        # Keep last 50 errors only
        profile.error_history = profile.error_history[-50:]

        await self._store.save(profile)
        return profile

    async def get_profile(self, user_id: str) -> Optional[UserProfile]:
        return await self._store.get(user_id)
```

## Solution 2: Repeat Question Detector

```python
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

@dataclass
class RepeatedQuestionPattern:
    user_id: str
    question_hash: str
    question_preview: str
    occurrence_count: int
    session_ids: List[str]
    first_seen: float
    last_seen: float
    was_ever_resolved: bool

class RepeatQuestionDetector:
    """
    Tracks questions per user across sessions.
    Identifies questions asked ≥2 times without a satisfactory resolution,
    indicating the agent is failing to meet a recurring need.
    """

    def __init__(self, store):
        self._store = store

    def _hash_question(self, text: str) -> str:
        # Normalize: lowercase, strip punctuation, hash
        import re
        normalized = re.sub(r'[^\w\s]', '', text.lower().strip())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    async def record(
        self,
        user_id: str,
        question: str,
        session_id: str,
        resolved: bool = False,
        timestamp: float = None,
    ) -> Optional[RepeatedQuestionPattern]:
        import time
        ts = timestamp or time.time()
        q_hash = self._hash_question(question)
        key = f"{user_id}:{q_hash}"

        pattern = await self._store.get(key)
        if pattern is None:
            pattern = RepeatedQuestionPattern(
                user_id=user_id,
                question_hash=q_hash,
                question_preview=question[:100],
                occurrence_count=1,
                session_ids=[session_id],
                first_seen=ts,
                last_seen=ts,
                was_ever_resolved=resolved,
            )
        else:
            if session_id not in pattern.session_ids:
                pattern.occurrence_count += 1
                pattern.session_ids.append(session_id)
            pattern.last_seen = ts
            pattern.was_ever_resolved = pattern.was_ever_resolved or resolved

        await self._store.set(key, pattern)
        if pattern.occurrence_count >= 2 and not pattern.was_ever_resolved:
            return pattern
        return None

    async def top_unresolved_repeats(self, limit: int = 20) -> List[RepeatedQuestionPattern]:
        """Returns questions most frequently repeated without resolution."""
        patterns = await self._store.scan_all()
        unresolved = [p for p in patterns if not p.was_ever_resolved and p.occurrence_count >= 2]
        return sorted(unresolved, key=lambda p: p.occurrence_count, reverse=True)[:limit]
```

## Solution 3: Behavioral Cohort Classifier

```python
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

class UserCohort(Enum):
    NEW_USER = "new_user"                  # < 3 sessions
    CASUAL = "casual"                      # 3–10 sessions, low tool usage
    REGULAR = "regular"                    # 10–50 sessions
    POWER_USER = "power_user"              # 50+ sessions, diverse tool usage
    AT_RISK = "at_risk"                    # declining activity + errors
    CHURNED = "churned"                    # no activity in 30+ days

@dataclass
class CohortAssignment:
    user_id: str
    cohort: UserCohort
    confidence: float
    signals: List[str]

class BehavioralCohortClassifier:
    """
    Classifies users into behavioral cohorts based on their cross-session profile.
    Rules-based for interpretability; replace with ML model for production scale.
    """

    def classify(self, profile: "UserProfile", current_time: float = None) -> CohortAssignment:
        import time
        now = current_time or time.time()
        signals = []
        days_since_last = (now - profile.last_seen) / 86400

        if days_since_last > 30:
            signals.append(f"inactive for {days_since_last:.0f} days")
            return CohortAssignment(profile.user_id, UserCohort.CHURNED, 0.9, signals)

        if profile.session_count < 3:
            signals.append(f"only {profile.session_count} sessions")
            return CohortAssignment(profile.user_id, UserCohort.NEW_USER, 0.95, signals)

        error_rate = len(profile.error_history) / max(profile.total_turns, 1)
        recent_activity_declining = (
            profile.session_count > 10
            and days_since_last > 7
            and error_rate > 0.2
        )
        if recent_activity_declining:
            signals.append(f"error_rate={error_rate:.1%}, inactive {days_since_last:.0f}d")
            return CohortAssignment(profile.user_id, UserCohort.AT_RISK, 0.8, signals)

        tool_diversity = len(profile.frequent_tools)
        if profile.session_count >= 50 and tool_diversity >= 5:
            signals.append(f"{profile.session_count} sessions, {tool_diversity} tools")
            return CohortAssignment(profile.user_id, UserCohort.POWER_USER, 0.85, signals)

        if profile.session_count >= 10:
            signals.append(f"{profile.session_count} sessions")
            return CohortAssignment(profile.user_id, UserCohort.REGULAR, 0.8, signals)

        signals.append(f"{profile.session_count} sessions, low diversity")
        return CohortAssignment(profile.user_id, UserCohort.CASUAL, 0.75, signals)

    def cohort_distribution(self, profiles: List["UserProfile"]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for p in profiles:
            assignment = self.classify(p)
            counts[assignment.cohort.value] = counts.get(assignment.cohort.value, 0) + 1
        return counts
```

## Solution 4: Sequential Pattern Miner (N-Gram Tool Sequences)

```python
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple

@dataclass
class ToolSequencePattern:
    sequence: Tuple[str, ...]
    count: int
    user_count: int
    avg_session_outcome: float  # success rate for sessions containing this sequence

class SequentialPatternMiner:
    """
    Mines frequent N-gram tool call sequences across all user sessions.
    Reveals common multi-step workflows and failure patterns.
    """

    def __init__(self, min_support: int = 5, max_ngram: int = 4):
        self._min_support = min_support
        self._max_ngram = max_ngram

    def mine(self, sessions: List["SessionSummary"]) -> List[ToolSequencePattern]:
        # Count N-grams across all sessions
        ngram_sessions: Dict[Tuple, List[str]] = defaultdict(list)
        ngram_outcomes: Dict[Tuple, List[float]] = defaultdict(list)

        for session in sessions:
            tools = session.tool_calls
            outcome_score = 1.0 if session.outcome == "completed" else 0.0

            for n in range(2, self._max_ngram + 1):
                for i in range(len(tools) - n + 1):
                    ngram = tuple(tools[i:i + n])
                    ngram_sessions[ngram].append(session.session_id)
                    ngram_outcomes[ngram].append(outcome_score)

        patterns = []
        for ngram, session_ids in ngram_sessions.items():
            unique_users = len(set(session_ids))
            if len(session_ids) >= self._min_support:
                outcomes = ngram_outcomes[ngram]
                patterns.append(ToolSequencePattern(
                    sequence=ngram,
                    count=len(session_ids),
                    user_count=unique_users,
                    avg_session_outcome=sum(outcomes) / len(outcomes),
                ))

        return sorted(patterns, key=lambda p: p.count, reverse=True)

    def failure_sequences(
        self, patterns: List[ToolSequencePattern], threshold: float = 0.3
    ) -> List[ToolSequencePattern]:
        """Return sequences with low success rate — likely failure workflows."""
        return [p for p in patterns if p.avg_session_outcome < threshold]
```

## Solution 5: Retention and Churn Cohort Tracker

```python
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class RetentionCohort:
    cohort_week: str        # ISO week: "2026-W14"
    initial_users: int
    retained_week_1: int
    retained_week_2: int
    retained_week_4: int
    retained_week_8: int

class RetentionCohortTracker:
    """
    Groups users by signup week and tracks what percentage
    return in subsequent weeks. Standard retention analysis.
    """

    def __init__(self, store):
        self._store = store

    def _iso_week(self, timestamp: float) -> str:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"

    def _weeks_since(self, first_seen: float, event_time: float) -> int:
        return int((event_time - first_seen) / (7 * 86400))

    async def record_activity(self, user_id: str, first_seen: float, activity_time: float) -> None:
        week_key = self._iso_week(first_seen)
        weeks_since = self._weeks_since(first_seen, activity_time)
        await self._store.mark_active(week_key, user_id, weeks_since)

    async def compute_cohort(self, cohort_week: str) -> Optional[RetentionCohort]:
        users = await self._store.get_cohort_users(cohort_week)
        if not users:
            return None

        def retained_at(week: int) -> int:
            return sum(
                1 for uid in users
                if await self._store.was_active(cohort_week, uid, week)
            )

        return RetentionCohort(
            cohort_week=cohort_week,
            initial_users=len(users),
            retained_week_1=await self._store.count_active(cohort_week, 1),
            retained_week_2=await self._store.count_active(cohort_week, 2),
            retained_week_4=await self._store.count_active(cohort_week, 4),
            retained_week_8=await self._store.count_active(cohort_week, 8),
        )
```

## Solution 6: Cross-Session Behavior Dashboard

```python
import time
from typing import Dict, List, Optional

class CrossSessionBehaviorDashboard:
    def __init__(
        self,
        aggregator: UserSessionAggregator,
        repeat_detector: RepeatQuestionDetector,
        classifier: BehavioralCohortClassifier,
        miner: SequentialPatternMiner,
    ):
        self._aggregator = aggregator
        self._repeat = repeat_detector
        self._classifier = classifier
        self._miner = miner

    async def weekly_report(self, sessions: List["SessionSummary"]) -> dict:
        profiles = []
        for session in sessions:
            profile = await self._aggregator.ingest_session(session)
            profiles.append(profile)

        cohort_dist = self._classifier.cohort_distribution(profiles)
        patterns = self._miner.mine(sessions)
        failure_seqs = self._miner.failure_sequences(patterns)
        unresolved = await self._repeat.top_unresolved_repeats(limit=10)

        return {
            "generated_at": time.time(),
            "sessions_analyzed": len(sessions),
            "unique_users": len(profiles),
            "cohort_distribution": cohort_dist,
            "top_tool_sequences": [
                {"sequence": list(p.sequence), "count": p.count, "success_rate": p.avg_session_outcome}
                for p in patterns[:10]
            ],
            "top_failure_sequences": [
                {"sequence": list(p.sequence), "count": p.count, "success_rate": p.avg_session_outcome}
                for p in failure_seqs[:5]
            ],
            "top_unresolved_questions": [
                {"question": q.question_preview, "occurrences": q.occurrence_count}
                for q in unresolved
            ],
        }
```

## Comparison

| Approach | Scope | Latency | Storage | Actionable Insights |
|---|---|---|---|---|
| UserSessionAggregator | Per-user rolling profile | Low (incremental) | Profile store | Tool/intent frequency |
| RepeatQuestionDetector | Per-user question history | Low | Key-value store | Unmet recurring needs |
| BehavioralCohortClassifier | Per-user classification | None (compute) | None | Cohort segmentation |
| SequentialPatternMiner | All sessions N-gram | Batch | In-memory | Workflow + failure patterns |
| RetentionCohortTracker | Signup-week cohorts | Batch | Cohort store | Retention/churn rates |
| CrossSessionBehaviorDashboard | Combined weekly | Batch | None | Full behavioral report |

**Best for production**: Run `UserSessionAggregator` incrementally after each session completes (low latency). Run `RepeatQuestionDetector` per message to detect unresolved recurring needs in real-time. Run `SequentialPatternMiner` and `CrossSessionBehaviorDashboard` weekly as a batch job. Feed `BehavioralCohortClassifier` results into personalization and onboarding flows.
