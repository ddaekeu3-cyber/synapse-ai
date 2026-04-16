---
title: "Agent Doesn't Implement Business Metric Tracking Alongside Technical Metrics"
description: "Six solutions for capturing business KPIs—conversion, task completion, user satisfaction—alongside latency and error rates in AI agent systems."
difficulty: intermediate
category: observability
tags: [business-metrics, kpi, tracking, analytics, observability, product]
---

# Agent Doesn't Implement Business Metric Tracking Alongside Technical Metrics

Technical metrics (latency, error rate, token count) tell you *how* the agent runs, but not *whether it delivers value*. An agent can be fast and error-free yet fail to complete user goals. Business metrics like task completion rate, goal achievement, and user satisfaction reveal the real picture. These six solutions instrument both layers in tandem.

## Solution 1: Dual-Layer Metric Collector

Track technical and business metrics in the same event pipeline; emit to separate sinks.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from anthropic import AsyncAnthropic


class TaskOutcome(Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    ABANDONED = "abandoned"


@dataclass
class TechnicalEvent:
    event_type: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    model: str
    error: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class BusinessEvent:
    event_type: str
    session_id: str
    user_id: str | None
    task_category: str
    outcome: TaskOutcome
    goal_achieved: bool
    turns_to_complete: int
    user_rating: float | None = None  # 1-5 if collected
    revenue_impact: float | None = None  # USD, if applicable
    timestamp: float = field(default_factory=time.time)


class DualLayerCollector:
    def __init__(self):
        self._technical: list[TechnicalEvent] = []
        self._business: list[BusinessEvent] = []

    def record_technical(self, event: TechnicalEvent):
        self._technical.append(event)
        # In production: emit to Prometheus/Datadog
        print(
            f"[TECH] {event.event_type} latency={event.latency_ms:.0f}ms "
            f"tokens={event.input_tokens}+{event.output_tokens}"
            + (f" error={event.error}" if event.error else "")
        )

    def record_business(self, event: BusinessEvent):
        self._business.append(event)
        # In production: emit to analytics warehouse (BigQuery, Segment, Amplitude)
        print(
            f"[BIZ] {event.event_type} outcome={event.outcome.value} "
            f"goal_achieved={event.goal_achieved} turns={event.turns_to_complete}"
            + (f" rating={event.user_rating}" if event.user_rating else "")
        )

    def summary(self) -> dict:
        if not self._business:
            return {}
        goal_rate = sum(1 for e in self._business if e.goal_achieved) / len(self._business)
        avg_turns = sum(e.turns_to_complete for e in self._business) / len(self._business)
        avg_latency = sum(e.latency_ms for e in self._technical) / max(len(self._technical), 1)
        avg_tokens = sum(e.input_tokens + e.output_tokens for e in self._technical) / max(len(self._technical), 1)
        return {
            "goal_achievement_rate": round(goal_rate, 3),
            "avg_turns_to_complete": round(avg_turns, 1),
            "avg_latency_ms": round(avg_latency, 1),
            "avg_tokens_per_request": round(avg_tokens, 1),
            "sessions": len(self._business),
        }


class InstrumentedTaskAgent:
    SYSTEM = """You are a task-completion assistant. When you have fully completed the user's task,
end your response with exactly: [TASK_COMPLETE]. If you cannot complete it, end with [TASK_FAILED]."""

    def __init__(self, collector: DualLayerCollector):
        self.client = AsyncAnthropic()
        self.collector = collector

    async def run_task(
        self,
        task: str,
        task_category: str,
        user_id: str | None = None,
        max_turns: int = 5,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())[:8]
        turns = 0
        goal_achieved = False
        messages = []
        total_input = total_output = 0

        while turns < max_turns:
            turns += 1
            user_msg = task if turns == 1 else "Continue."
            messages.append({"role": "user", "content": user_msg})

            start = time.perf_counter()
            error = None
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    system=self.SYSTEM,
                    messages=messages,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                text = response.content[0].text
                total_input += response.usage.input_tokens
                total_output += response.usage.output_tokens

                self.collector.record_technical(TechnicalEvent(
                    event_type="llm_request",
                    latency_ms=elapsed_ms,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model="claude-haiku-4-5-20251001",
                ))

                messages.append({"role": "assistant", "content": text})

                if "[TASK_COMPLETE]" in text:
                    goal_achieved = True
                    outcome = TaskOutcome.SUCCESS
                    break
                elif "[TASK_FAILED]" in text:
                    outcome = TaskOutcome.FAILURE
                    break
            except Exception as e:
                elapsed_ms = (time.perf_counter() - start) * 1000
                error = str(e)
                self.collector.record_technical(TechnicalEvent(
                    event_type="llm_request",
                    latency_ms=elapsed_ms,
                    input_tokens=0,
                    output_tokens=0,
                    model="claude-haiku-4-5-20251001",
                    error=error,
                ))
                outcome = TaskOutcome.FAILURE
                break
        else:
            outcome = TaskOutcome.PARTIAL

        self.collector.record_business(BusinessEvent(
            event_type="task_session",
            session_id=session_id,
            user_id=user_id,
            task_category=task_category,
            outcome=outcome,
            goal_achieved=goal_achieved,
            turns_to_complete=turns,
        ))

        return {
            "session_id": session_id,
            "outcome": outcome.value,
            "goal_achieved": goal_achieved,
            "turns": turns,
            "total_tokens": total_input + total_output,
        }


async def demo_dual_layer():
    collector = DualLayerCollector()
    agent = InstrumentedTaskAgent(collector)

    tasks = [
        ("Write a haiku about Python programming.", "creative", "user_1"),
        ("What is 17 * 23?", "math", "user_2"),
        ("Explain recursion in one sentence.", "education", "user_1"),
    ]
    for task, category, uid in tasks:
        result = await agent.run_task(task, category, uid)
        print(f"Result: {result}\n")

    print("=== Summary ===")
    print(collector.summary())
```

## Solution 2: Funnel Metric Tracking for Multi-Step Agent Flows

Track drop-off at each step of a multi-step agent workflow; measure funnel conversion.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class FunnelStep:
    name: str
    started_at: float = 0.0
    completed_at: float | None = None
    skipped: bool = False
    error: str | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.completed_at:
            return (self.completed_at - self.started_at) * 1000
        return None

    @property
    def succeeded(self) -> bool:
        return self.completed_at is not None and not self.error


@dataclass
class FunnelSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    steps: list[FunnelStep] = field(default_factory=list)
    user_id: str | None = None
    converted: bool = False  # True if user completed the entire flow

    def start_step(self, name: str) -> FunnelStep:
        step = FunnelStep(name=name, started_at=time.time())
        self.steps.append(step)
        return step

    def complete_step(self, step: FunnelStep):
        step.completed_at = time.time()

    def fail_step(self, step: FunnelStep, error: str):
        step.error = error
        step.completed_at = time.time()

    def conversion_summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "converted": self.converted,
            "steps": [
                {
                    "name": s.name,
                    "succeeded": s.succeeded,
                    "duration_ms": s.duration_ms,
                    "error": s.error,
                }
                for s in self.steps
            ],
            "total_duration_ms": sum(
                s.duration_ms or 0 for s in self.steps
            ),
        }


class FunnelAggregator:
    def __init__(self):
        self._sessions: list[FunnelSession] = []

    def record(self, session: FunnelSession):
        self._sessions.append(session)

    def funnel_report(self) -> dict:
        if not self._sessions:
            return {}
        # Count sessions reaching each step
        step_names: list[str] = []
        for s in self._sessions:
            for step in s.steps:
                if step.name not in step_names:
                    step_names.append(step.name)

        funnel: dict[str, dict] = {}
        for name in step_names:
            reached = sum(1 for s in self._sessions if any(st.name == name for st in s.steps))
            completed = sum(
                1 for s in self._sessions
                if any(st.name == name and st.succeeded for st in s.steps)
            )
            funnel[name] = {
                "reached": reached,
                "completed": completed,
                "completion_rate": round(completed / max(reached, 1), 3),
            }
        conversion_rate = sum(1 for s in self._sessions if s.converted) / len(self._sessions)
        return {"funnel": funnel, "overall_conversion": round(conversion_rate, 3)}


AGGREGATOR = FunnelAggregator()


class FunnelTrackedAgent:
    """Agent for a 3-step workflow: understand → plan → execute."""

    def __init__(self, aggregator: FunnelAggregator = AGGREGATOR):
        self.client = AsyncAnthropic()
        self.agg = aggregator

    async def _llm(self, system: str, message: str) -> str:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text

    async def run(self, user_request: str, user_id: str | None = None) -> str | None:
        session = FunnelSession(user_id=user_id)

        # Step 1: Understand intent
        step = session.start_step("understand_intent")
        try:
            intent = await self._llm(
                "Extract the user's core intent in one sentence.",
                user_request,
            )
            session.complete_step(step)
        except Exception as e:
            session.fail_step(step, str(e))
            self.agg.record(session)
            return None

        # Step 2: Generate plan
        step = session.start_step("generate_plan")
        try:
            plan = await self._llm(
                "Given an intent, produce a 3-step action plan as a numbered list.",
                f"Intent: {intent}",
            )
            session.complete_step(step)
        except Exception as e:
            session.fail_step(step, str(e))
            self.agg.record(session)
            return None

        # Step 3: Execute
        step = session.start_step("execute")
        try:
            result = await self._llm(
                "Execute this plan and produce the final output.",
                f"Plan:\n{plan}\n\nOriginal request: {user_request}",
            )
            session.complete_step(step)
            session.converted = True
            self.agg.record(session)
            return result
        except Exception as e:
            session.fail_step(step, str(e))
            self.agg.record(session)
            return None


async def demo_funnel():
    agent = FunnelTrackedAgent()
    requests = [
        ("Summarize the benefits of exercise.", "u1"),
        ("Write a Python function to reverse a string.", "u2"),
        ("Explain machine learning to a 5-year-old.", "u1"),
    ]
    for req, uid in requests:
        await agent.run(req, uid)

    print(AGGREGATOR.funnel_report())
```

## Solution 3: User Satisfaction Signal Collection with LLM-as-Judge

When explicit user ratings aren't available, use an LLM judge to estimate satisfaction; track over time.

```python
import asyncio
import time
import statistics
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class SatisfactionSignal:
    session_id: str
    user_message: str
    agent_response: str
    explicit_rating: float | None  # 1-5, from user
    implicit_score: float | None   # 0-1, from LLM judge
    combined_score: float          # Weighted combination
    timestamp: float = field(default_factory=time.time)

    @staticmethod
    def combine(explicit: float | None, implicit: float | None) -> float:
        if explicit is not None and implicit is not None:
            # Weight explicit 2:1 over implicit
            explicit_norm = (explicit - 1) / 4  # Normalize 1-5 -> 0-1
            return 0.67 * explicit_norm + 0.33 * implicit
        elif explicit is not None:
            return (explicit - 1) / 4
        elif implicit is not None:
            return implicit
        return 0.5


class SatisfactionTracker:
    def __init__(self):
        self._signals: list[SatisfactionSignal] = []

    def record(self, signal: SatisfactionSignal):
        self._signals.append(signal)

    def rolling_satisfaction(self, window: int = 100) -> float:
        recent = self._signals[-window:]
        if not recent:
            return 0.0
        return statistics.mean(s.combined_score for s in recent)

    def satisfaction_trend(self, buckets: int = 5) -> list[float]:
        if not self._signals:
            return []
        n = len(self._signals)
        size = max(1, n // buckets)
        return [
            statistics.mean(s.combined_score for s in self._signals[i:i+size])
            for i in range(0, n, size)
        ][:buckets]

    def report(self) -> dict:
        if not self._signals:
            return {}
        scores = [s.combined_score for s in self._signals]
        explicit_count = sum(1 for s in self._signals if s.explicit_rating is not None)
        return {
            "total_sessions": len(self._signals),
            "mean_satisfaction": round(statistics.mean(scores), 3),
            "p25": round(sorted(scores)[len(scores) // 4], 3),
            "p75": round(sorted(scores)[3 * len(scores) // 4], 3),
            "explicit_rating_coverage": round(explicit_count / len(self._signals), 3),
            "trend": self.satisfaction_trend(),
        }


class SatisfactionTrackedAgent:
    JUDGE_SYSTEM = """Rate how satisfying this AI response is to the user's request.
Consider: completeness, accuracy, helpfulness, clarity.
Reply with ONLY a decimal 0.0 to 1.0. No other text."""

    def __init__(self, tracker: SatisfactionTracker):
        self.client = AsyncAnthropic()
        self.tracker = tracker

    async def _judge_satisfaction(self, user_msg: str, response: str) -> float:
        try:
            judge = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                system=self.JUDGE_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": f"User: {user_msg[:200]}\nResponse: {response[:400]}",
                }],
            )
            return float(judge.content[0].text.strip())
        except (ValueError, Exception):
            return 0.5

    async def chat(
        self,
        message: str,
        session_id: str,
        explicit_rating: float | None = None,
    ) -> str:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text

        # Async judge — doesn't block response delivery
        implicit = await self._judge_satisfaction(message, text)
        combined = SatisfactionSignal.combine(explicit_rating, implicit)

        self.tracker.record(SatisfactionSignal(
            session_id=session_id,
            user_message=message,
            agent_response=text,
            explicit_rating=explicit_rating,
            implicit_score=implicit,
            combined_score=combined,
        ))
        return text


async def demo_satisfaction():
    tracker = SatisfactionTracker()
    agent = SatisfactionTrackedAgent(tracker)

    convos = [
        ("What is 2+2?", "s1", 5.0),
        ("Explain blockchain in simple terms.", "s2", None),
        ("Write a poem about clouds.", "s3", 4.0),
        ("What is the capital of France?", "s4", None),
        ("How do I sort a list in Python?", "s5", 5.0),
    ]
    for msg, sid, rating in convos:
        await agent.chat(msg, sid, rating)

    print(tracker.report())
    print(f"Rolling satisfaction (last 5): {tracker.rolling_satisfaction(5):.3f}")
```

## Solution 4: Revenue and Cost Attribution per Agent Session

Track token cost alongside estimated business value (leads, conversions, support deflections).

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

# Token pricing (USD per 1M tokens)
PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}


@dataclass
class SessionEconomics:
    session_id: str
    channel: str  # "support", "sales", "onboarding"
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "claude-haiku-4-5-20251001"
    converted: bool = False
    deflected_human_support: bool = False
    lead_qualified: bool = False

    @property
    def token_cost_usd(self) -> float:
        p = PRICING.get(self.model, {"input": 1.0, "output": 5.0})
        return (
            self.input_tokens * p["input"] / 1_000_000
            + self.output_tokens * p["output"] / 1_000_000
        )

    @property
    def estimated_value_usd(self) -> float:
        value = 0.0
        if self.converted:
            value += 50.0          # Avg order value
        if self.deflected_human_support:
            value += 12.0          # Avg cost of human support ticket
        if self.lead_qualified:
            value += 25.0          # Avg value of qualified lead
        return value

    @property
    def roi(self) -> float:
        cost = self.token_cost_usd
        if cost == 0:
            return 0.0
        return (self.estimated_value_usd - cost) / cost


class EconomicsTracker:
    def __init__(self):
        self._sessions: list[SessionEconomics] = []

    def record(self, session: SessionEconomics):
        self._sessions.append(session)

    def report(self) -> dict:
        if not self._sessions:
            return {}
        total_cost = sum(s.token_cost_usd for s in self._sessions)
        total_value = sum(s.estimated_value_usd for s in self._sessions)
        by_channel: dict[str, dict] = {}
        for s in self._sessions:
            ch = by_channel.setdefault(s.channel, {"cost": 0.0, "value": 0.0, "sessions": 0})
            ch["cost"] += s.token_cost_usd
            ch["value"] += s.estimated_value_usd
            ch["sessions"] += 1
        return {
            "total_sessions": len(self._sessions),
            "total_cost_usd": round(total_cost, 4),
            "total_estimated_value_usd": round(total_value, 2),
            "overall_roi": round((total_value - total_cost) / max(total_cost, 0.0001), 1),
            "conversion_rate": round(
                sum(1 for s in self._sessions if s.converted) / len(self._sessions), 3
            ),
            "support_deflection_rate": round(
                sum(1 for s in self._sessions if s.deflected_human_support) / len(self._sessions), 3
            ),
            "by_channel": {
                ch: {**data, "roi": round((data["value"] - data["cost"]) / max(data["cost"], 0.0001), 1)}
                for ch, data in by_channel.items()
            },
        }


class EconomicsAgent:
    SUPPORT_SYSTEM = """You are a customer support agent. Resolve issues completely.
If fully resolved without human escalation, end with [DEFLECTED].
If converting to a sale, end with [CONVERTED]."""

    def __init__(self, tracker: EconomicsTracker):
        self.client = AsyncAnthropic()
        self.tracker = tracker

    async def handle(self, message: str, channel: str, session_id: str) -> str:
        session = SessionEconomics(session_id=session_id, channel=channel)
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=self.SUPPORT_SYSTEM,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text
        session.input_tokens += response.usage.input_tokens
        session.output_tokens += response.usage.output_tokens
        session.deflected_human_support = "[DEFLECTED]" in text
        session.converted = "[CONVERTED]" in text
        self.tracker.record(session)
        return text


async def demo_economics():
    tracker = EconomicsTracker()
    agent = EconomicsAgent(tracker)

    sessions = [
        ("How do I reset my password?", "support", "s1"),
        ("I'd like to upgrade my plan.", "sales", "s2"),
        ("My payment failed.", "support", "s3"),
        ("What features does the pro plan include?", "sales", "s4"),
    ]
    for msg, channel, sid in sessions:
        await agent.handle(msg, channel, sid)

    print(tracker.report())
```

## Solution 5: Goal Achievement Rate with Cohort Segmentation

Measure goal achievement rates segmented by user cohort, task type, and time period.

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict
from anthropic import AsyncAnthropic


@dataclass
class GoalEvent:
    session_id: str
    cohort: str              # e.g., "new_user", "power_user", "enterprise"
    task_type: str           # e.g., "coding", "writing", "analysis"
    goal_achieved: bool
    turns_taken: int
    timestamp: float = field(default_factory=time.time)

    @property
    def week(self) -> str:
        import datetime
        dt = datetime.datetime.fromtimestamp(self.timestamp)
        return dt.strftime("%Y-W%W")


class CohortAnalytics:
    def __init__(self):
        self._events: list[GoalEvent] = []

    def record(self, event: GoalEvent):
        self._events.append(event)

    def achievement_by_cohort(self) -> dict[str, dict]:
        by_cohort: dict[str, list[GoalEvent]] = defaultdict(list)
        for e in self._events:
            by_cohort[e.cohort].append(e)
        return {
            cohort: {
                "sessions": len(events),
                "goal_achievement_rate": round(
                    sum(1 for e in events if e.goal_achieved) / len(events), 3
                ),
                "avg_turns": round(sum(e.turns_taken for e in events) / len(events), 1),
            }
            for cohort, events in by_cohort.items()
        }

    def achievement_by_task_type(self) -> dict[str, dict]:
        by_type: dict[str, list[GoalEvent]] = defaultdict(list)
        for e in self._events:
            by_type[e.task_type].append(e)
        return {
            task: {
                "sessions": len(events),
                "goal_achievement_rate": round(
                    sum(1 for e in events if e.goal_achieved) / len(events), 3
                ),
            }
            for task, events in by_type.items()
        }

    def weekly_trend(self) -> dict[str, float]:
        by_week: dict[str, list[bool]] = defaultdict(list)
        for e in self._events:
            by_week[e.week].append(e.goal_achieved)
        return {
            week: round(sum(achieved) / len(achieved), 3)
            for week, achieved in sorted(by_week.items())
        }


COHORT_ANALYTICS = CohortAnalytics()


def classify_cohort(user_id: str | None) -> str:
    """Stub: in production, look up user segment from CRM."""
    if user_id is None:
        return "anonymous"
    h = hash(user_id) % 3
    return ["new_user", "power_user", "enterprise"][h]


def classify_task(message: str) -> str:
    msg = message.lower()
    if any(kw in msg for kw in ["code", "function", "python", "bug", "script"]):
        return "coding"
    if any(kw in msg for kw in ["write", "essay", "email", "draft", "poem"]):
        return "writing"
    if any(kw in msg for kw in ["analyze", "compare", "explain", "summarize"]):
        return "analysis"
    return "general"


class CohortTrackedAgent:
    SYSTEM = """Complete the user's task. End with [ACHIEVED] if you fully completed it."""

    def __init__(self, analytics: CohortAnalytics = COHORT_ANALYTICS):
        self.client = AsyncAnthropic()
        self.analytics = analytics

    async def chat(self, message: str, user_id: str | None = None) -> str:
        import uuid
        session_id = str(uuid.uuid4())[:8]
        cohort = classify_cohort(user_id)
        task_type = classify_task(message)

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=self.SYSTEM,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text
        self.analytics.record(GoalEvent(
            session_id=session_id,
            cohort=cohort,
            task_type=task_type,
            goal_achieved="[ACHIEVED]" in text,
            turns_taken=1,
        ))
        return text


async def demo_cohort():
    agent = CohortTrackedAgent()
    tasks = [
        ("Write a Python function to find prime numbers.", "user_a"),
        ("Analyze the pros and cons of remote work.", "user_b"),
        ("Draft a professional email declining a meeting.", "user_c"),
        ("Explain what a neural network is.", None),
        ("Debug this: print('hello'", "user_a"),
    ]
    for msg, uid in tasks:
        await agent.chat(msg, uid)

    print("=== By Cohort ===")
    print(COHORT_ANALYTICS.achievement_by_cohort())
    print("\n=== By Task Type ===")
    print(COHORT_ANALYTICS.achievement_by_task_type())
```

## Solution 6: Real-Time Business Dashboard with FastAPI

Serve live business and technical metrics via a FastAPI dashboard endpoint.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from collections import deque
from fastapi import FastAPI
from anthropic import AsyncAnthropic
import uvicorn


@dataclass
class SessionRecord:
    session_id: str
    task: str
    model: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    goal_achieved: bool
    satisfaction_score: float
    cost_usd: float
    timestamp: float = field(default_factory=time.time)


class BusinessDashboard:
    def __init__(self, window: int = 500):
        self._window = window
        self._records: deque[SessionRecord] = deque(maxlen=window)
        self._total_sessions = 0

    def record(self, r: SessionRecord):
        self._records.append(r)
        self._total_sessions += 1

    def metrics(self) -> dict:
        recent = list(self._records)
        if not recent:
            return {"status": "no data"}
        n = len(recent)
        return {
            "window_sessions": n,
            "total_sessions": self._total_sessions,
            "technical": {
                "avg_latency_ms": round(sum(r.latency_ms for r in recent) / n, 1),
                "avg_input_tokens": round(sum(r.input_tokens for r in recent) / n, 1),
                "avg_output_tokens": round(sum(r.output_tokens for r in recent) / n, 1),
                "total_cost_usd": round(sum(r.cost_usd for r in recent), 4),
            },
            "business": {
                "goal_achievement_rate": round(
                    sum(1 for r in recent if r.goal_achieved) / n, 3
                ),
                "avg_satisfaction": round(
                    sum(r.satisfaction_score for r in recent) / n, 3
                ),
                "estimated_value_usd": round(
                    sum(r.satisfaction_score * 5 for r in recent if r.goal_achieved), 2
                ),
            },
            "computed_at": time.time(),
        }


dashboard = BusinessDashboard()
app = FastAPI(title="Agent Business Metrics")
client = AsyncAnthropic()

PRICING_INPUT = 0.80 / 1_000_000
PRICING_OUTPUT = 4.00 / 1_000_000


@app.get("/metrics")
async def get_metrics():
    return dashboard.metrics()


@app.post("/chat")
async def chat(body: dict):
    message = body.get("message", "")
    start = time.perf_counter()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system="Complete the task. End with [DONE] if fully completed.",
        messages=[{"role": "user", "content": message}],
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    text = response.content[0].text
    cost = (
        response.usage.input_tokens * PRICING_INPUT
        + response.usage.output_tokens * PRICING_OUTPUT
    )
    # Satisfaction heuristic: length + completion signal
    satisfaction = min(1.0, len(text) / 500) * (1.1 if "[DONE]" in text else 0.8)

    dashboard.record(SessionRecord(
        session_id=str(uuid.uuid4())[:8],
        task=message[:100],
        model="claude-haiku-4-5-20251001",
        latency_ms=elapsed_ms,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        goal_achieved="[DONE]" in text,
        satisfaction_score=min(1.0, satisfaction),
        cost_usd=cost,
    ))
    return {"response": text, "latency_ms": round(elapsed_ms, 1)}


# Run: uvicorn module:app --reload
# Dashboard: GET /metrics
```

## Comparison Table

| Solution | Business Signal | Technical Signal | Cohort Support | Real-Time | Best For |
|---|---|---|---|---|---|
| Dual-Layer Collector | Task outcome, goal | Latency, tokens | No | Yes (print) | Baseline dual instrumentation |
| Funnel Tracking | Step conversion, drop-off | Per-step latency | No | Yes | Multi-step agent flows |
| Satisfaction Signals | LLM-judged + explicit rating | No | No | Yes | Conversational agents |
| Revenue Attribution | Conversion, deflection, ROI | Token cost | By channel | No (batch) | Monetized agent products |
| Cohort Achievement | Goal rate by segment | No | Yes | No (batch) | Product analytics & A/B testing |
| Business Dashboard | Achievement, satisfaction | Latency, cost | No | Yes (API) | Live operations monitoring |

**Recommended**: Start with **Dual-Layer Collector** (Solution 1) to establish the baseline event schema, then layer in **Funnel Tracking** (Solution 2) for multi-step flows and **Revenue Attribution** (Solution 4) once you have business value signals. Expose everything through the **Business Dashboard** (Solution 6) for real-time operations visibility.
