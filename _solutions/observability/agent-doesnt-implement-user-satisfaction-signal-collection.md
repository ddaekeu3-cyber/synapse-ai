---
title: "Agent Doesn't Implement User Satisfaction Signal Collection"
description: "Collect explicit and implicit satisfaction signals from users to measure agent quality, detect regressions, and drive continuous improvement without waiting for formal evaluations."
difficulty: intermediate
category: observability
tags: [user-satisfaction, feedback, signals, observability, quality]
---

## Problem

Agent quality is measured by internal metrics—token counts, latency, error rates—but not by whether users actually got what they needed. Without satisfaction signals, improvements are guesses, regressions go undetected until users churn, and there's no feedback loop to improve prompts, models, or routing decisions.

## Solutions

### Option 1: Explicit Thumbs Up/Down with Structured Storage

Collect binary feedback tied to conversation IDs and store with response metadata for analysis.

```python
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
FEEDBACK_LOG = Path("feedback_log.jsonl")

async def get_agent_response(user_message: str, session_id: str) -> tuple[str, str]:
    response_id = str(uuid.uuid4())
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": user_message}]
    )
    text = response.content[0].text

    # Log response metadata immediately
    record = {
        "response_id": response_id,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_message": user_message,
        "response_preview": text[:100],
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "feedback": None,
    }
    with open(FEEDBACK_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")

    return text, response_id

def record_feedback(response_id: str, rating: int, comment: str = ""):
    """rating: 1 = thumbs up, -1 = thumbs down, 0 = neutral"""
    records = []
    if FEEDBACK_LOG.exists():
        with open(FEEDBACK_LOG) as f:
            records = [json.loads(line) for line in f if line.strip()]

    updated = False
    for record in records:
        if record["response_id"] == response_id:
            record["feedback"] = {"rating": rating, "comment": comment,
                                  "recorded_at": datetime.now(timezone.utc).isoformat()}
            updated = True
            break

    if updated:
        with open(FEEDBACK_LOG, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

def satisfaction_summary() -> dict:
    if not FEEDBACK_LOG.exists():
        return {}
    records = [json.loads(l) for l in FEEDBACK_LOG.read_text().splitlines() if l.strip()]
    rated = [r for r in records if r.get("feedback")]
    if not rated:
        return {"total_responses": len(records), "rated": 0}

    positive = sum(1 for r in rated if r["feedback"]["rating"] > 0)
    negative = sum(1 for r in rated if r["feedback"]["rating"] < 0)
    return {
        "total_responses": len(records),
        "rated": len(rated),
        "positive": positive,
        "negative": negative,
        "satisfaction_rate": positive / len(rated) if rated else 0,
    }

async def demo_explicit_feedback():
    session = str(uuid.uuid4())

    q1 = "What is a Python decorator?"
    resp1, rid1 = await get_agent_response(q1, session)
    print(f"Response: {resp1[:100]}...")
    # Simulate user clicking thumbs up
    record_feedback(rid1, 1, "Clear and concise")

    q2 = "Explain quantum entanglement in simple terms"
    resp2, rid2 = await get_agent_response(q2, session)
    print(f"Response: {resp2[:100]}...")
    # Simulate user clicking thumbs down
    record_feedback(rid2, -1, "Too technical")

    summary = satisfaction_summary()
    print(f"\nSatisfaction summary: {json.dumps(summary, indent=2)}")

asyncio.run(demo_explicit_feedback())
```

### Option 2: Implicit Signal Detection from Follow-up Behavior

Infer satisfaction from what users do next—rephrasing, asking follow-ups, or going silent.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum

client = AsyncAnthropic()

class ImplicitSignal(Enum):
    SATISFIED = "satisfied"           # User moved to new topic
    CLARIFICATION_NEEDED = "clarification"  # User asked follow-up on same topic
    REPHRASED = "rephrased"           # User repeated question differently
    ABANDONED = "abandoned"           # User went silent
    QUICK_ACK = "quick_ack"          # Short "ok"/"thanks" indicates done

@dataclass
class Turn:
    user_message: str
    agent_response: str
    timestamp: float
    response_id: str
    inferred_signal: ImplicitSignal | None = None

@dataclass
class ConversationTracker:
    session_id: str
    turns: list[Turn] = field(default_factory=list)

    ACK_PATTERNS = {"ok", "thanks", "got it", "understood", "ty", "thx", "perfect", "great"}
    REPHRASE_SIMILARITY_THRESHOLD = 0.6

    def _is_quick_ack(self, message: str) -> bool:
        words = set(message.lower().strip().rstrip("!.,").split())
        return len(words) <= 3 and bool(words & self.ACK_PATTERNS)

    def _topics_overlap(self, msg1: str, msg2: str) -> float:
        """Simple word-overlap similarity."""
        words1 = set(msg1.lower().split()) - {"what", "how", "is", "the", "a", "an", "i", "you"}
        words2 = set(msg2.lower().split()) - {"what", "how", "is", "the", "a", "an", "i", "you"}
        if not words1 or not words2:
            return 0.0
        return len(words1 & words2) / min(len(words1), len(words2))

    def infer_signal_for_previous(self, new_message: str) -> ImplicitSignal | None:
        if not self.turns:
            return None

        last_turn = self.turns[-1]
        overlap = self._topics_overlap(last_turn.user_message, new_message)

        if self._is_quick_ack(new_message):
            return ImplicitSignal.QUICK_ACK
        elif overlap > self.REPHRASE_SIMILARITY_THRESHOLD:
            # High overlap = rephrase or clarification
            if len(new_message) < len(last_turn.user_message) * 0.8:
                return ImplicitSignal.REPHRASED
            return ImplicitSignal.CLARIFICATION_NEEDED
        else:
            return ImplicitSignal.SATISFIED

    def check_abandonment(self, timeout_seconds: float = 300.0) -> bool:
        if not self.turns:
            return False
        return (time.monotonic() - self.turns[-1].timestamp) > timeout_seconds

    def satisfaction_score(self) -> float:
        """0.0 to 1.0 based on inferred signals."""
        scored = [t for t in self.turns if t.inferred_signal]
        if not scored:
            return 0.5  # Unknown

        weights = {
            ImplicitSignal.SATISFIED: 1.0,
            ImplicitSignal.QUICK_ACK: 0.9,
            ImplicitSignal.CLARIFICATION_NEEDED: 0.5,
            ImplicitSignal.REPHRASED: 0.2,
            ImplicitSignal.ABANDONED: 0.1,
        }
        return sum(weights[t.inferred_signal] for t in scored) / len(scored)

class ImplicitFeedbackAgent:
    def __init__(self):
        self.tracker = ConversationTracker(session_id="demo-session")

    async def respond(self, user_message: str) -> str:
        import uuid

        # Infer signal for previous turn based on new message
        if self.tracker.turns:
            signal = self.tracker.infer_signal_for_previous(user_message)
            self.tracker.turns[-1].inferred_signal = signal

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[
                {"role": "user", "content": t.user_message} if i % 2 == 0
                else {"role": "assistant", "content": t.agent_response}
                for i, t in enumerate(self.tracker.turns)
            ] + [{"role": "user", "content": user_message}]
        )
        text = response.content[0].text

        turn = Turn(
            user_message=user_message,
            agent_response=text,
            timestamp=time.monotonic(),
            response_id=str(uuid.uuid4())
        )
        self.tracker.turns.append(turn)
        return text

async def demo_implicit_signals():
    agent = ImplicitFeedbackAgent()

    conversation = [
        "What is a load balancer?",
        "Can you explain it more simply?",   # -> CLARIFICATION_NEEDED for turn 1
        "Got it, thanks!",                    # -> QUICK_ACK for turn 2
        "How do microservices differ from monoliths?",  # -> SATISFIED for turn 3
    ]

    for msg in conversation:
        response = await agent.respond(msg)
        print(f"User: {msg}")
        print(f"Agent: {response[:100]}...\n")

    score = agent.tracker.satisfaction_score()
    print(f"Session satisfaction score: {score:.2f}")
    for turn in agent.tracker.turns:
        if turn.inferred_signal:
            print(f"  '{turn.user_message[:40]}...' -> {turn.inferred_signal.value}")

asyncio.run(demo_implicit_signals())
```

### Option 3: Micro-Survey Injection at Natural Breakpoints

Insert lightweight rating prompts at conversation end or after complex tasks.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class SurveyConfig:
    trigger_after_turns: int = 5      # Survey after N turns
    trigger_on_task_complete: bool = True
    max_surveys_per_session: int = 2
    survey_style: str = "minimal"     # "minimal" | "scale" | "csat"

SURVEY_PROMPTS = {
    "minimal": "Quick check: did this answer your question? [yes / no / partially]",
    "scale": "Rate this response 1-5 (1=unhelpful, 5=excellent):",
    "csat": "How satisfied are you with this answer?\n  [😞 Very dissatisfied] [😐 Neutral] [😊 Very satisfied]",
}

TASK_COMPLETION_INDICATORS = [
    "thank", "perfect", "that's all", "done", "got it", "exactly",
    "works", "solved", "fixed", "deployed", "implemented"
]

class SurveyingAgent:
    def __init__(self, config: SurveyConfig):
        self.config = config
        self.turns = 0
        self.surveys_sent = 0
        self.survey_responses: list[dict] = []
        self.messages: list[dict] = []

    def _should_survey(self, last_user_message: str) -> bool:
        if self.surveys_sent >= self.config.max_surveys_per_session:
            return False
        if self.turns > 0 and self.turns % self.config.trigger_after_turns == 0:
            return True
        if self.config.trigger_on_task_complete:
            lower = last_user_message.lower()
            if any(indicator in lower for indicator in TASK_COMPLETION_INDICATORS):
                return True
        return False

    async def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        self.turns += 1

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=self.messages
        )
        text = response.content[0].text
        self.messages.append({"role": "assistant", "content": text})

        if self._should_survey(user_message):
            self.surveys_sent += 1
            survey_prompt = SURVEY_PROMPTS[self.config.survey_style]
            # In a real system, deliver this via the UI separately
            return text + f"\n\n---\n*{survey_prompt}*"

        return text

    def record_survey_response(self, turn: int, answer: str):
        self.survey_responses.append({"turn": turn, "answer": answer})

    def csat_score(self) -> float | None:
        """Rough CSAT from text responses."""
        positive = {"yes", "5", "😊", "great", "perfect", "satisfied"}
        negative = {"no", "1", "2", "😞", "bad", "unhelpful"}
        scored = []
        for r in self.survey_responses:
            lower = r["answer"].lower()
            if any(p in lower for p in positive):
                scored.append(1.0)
            elif any(n in lower for n in negative):
                scored.append(0.0)
            elif "partially" in lower or "3" in lower or "4" in lower:
                scored.append(0.5)
        return sum(scored) / len(scored) if scored else None

async def demo_micro_survey():
    config = SurveyConfig(trigger_after_turns=3, survey_style="minimal")
    agent = SurveyingAgent(config)

    interactions = [
        ("How do I parse JSON in Python?", None),
        ("What about handling errors?", None),
        ("How do I write JSON to a file?", None),
        ("Perfect, that's all I needed!", "yes"),  # Survey triggered here
    ]

    for user_msg, survey_response in interactions:
        response = await agent.chat(user_msg)
        print(f"User: {user_msg}")
        print(f"Agent: {response[:150]}")
        if survey_response:
            agent.record_survey_response(agent.turns, survey_response)
            print(f"[User survey response: {survey_response}]")
        print()

    score = agent.csat_score()
    print(f"CSAT score: {score:.0%}" if score is not None else "No CSAT data yet")

asyncio.run(demo_micro_survey())
```

### Option 4: Response Quality Heuristics Dashboard

Compute proxy metrics for quality—length appropriateness, specificity, code presence—without requiring user input.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class QualitySignals:
    response_id: str
    question: str
    response_length: int
    has_code: bool
    has_list: bool
    question_words_answered: float   # fraction of question keywords in response
    hedge_word_count: int            # "maybe", "perhaps", "might", etc.
    refusal_detected: bool

    def quality_score(self) -> float:
        score = 0.5  # baseline

        # Length appropriateness (penalize very short or very long)
        q_len = len(self.question.split())
        r_len = len(self.response_length.__str__())
        if 50 <= self.response_length <= 800:
            score += 0.15
        elif self.response_length < 20:
            score -= 0.25

        # Answering the question
        score += self.question_words_answered * 0.2

        # Hedging reduces confidence
        score -= min(self.hedge_word_count * 0.05, 0.2)

        # Refusal is negative
        if self.refusal_detected:
            score -= 0.4

        return max(0.0, min(1.0, score))

HEDGE_WORDS = {"maybe", "perhaps", "might", "could", "possibly", "uncertain",
               "not sure", "i think", "i believe", "it seems"}
REFUSAL_PATTERNS = [r"(?i)i (can'?t|cannot|am unable to|won'?t|will not)"]

def analyze_response(response_id: str, question: str, response: str) -> QualitySignals:
    q_words = set(re.sub(r'[^\w\s]', '', question.lower()).split()) - \
              {"what", "how", "why", "is", "are", "the", "a", "an", "do", "i"}
    r_lower = response.lower()

    answered = sum(1 for w in q_words if w in r_lower) / max(len(q_words), 1)
    hedge_count = sum(1 for h in HEDGE_WORDS if h in r_lower)
    refusal = any(re.search(p, response) for p in REFUSAL_PATTERNS)

    return QualitySignals(
        response_id=response_id,
        question=question,
        response_length=len(response),
        has_code="```" in response,
        has_list=bool(re.search(r'^\s*[-*\d]', response, re.MULTILINE)),
        question_words_answered=answered,
        hedge_word_count=hedge_count,
        refusal_detected=refusal,
    )

@dataclass
class QualityDashboard:
    signals: list[QualitySignals] = field(default_factory=list)

    def add(self, signal: QualitySignals):
        self.signals.append(signal)

    def report(self):
        if not self.signals:
            print("No signals collected.")
            return

        avg_score = sum(s.quality_score() for s in self.signals) / len(self.signals)
        code_rate = sum(1 for s in self.signals if s.has_code) / len(self.signals)
        avg_hedge = sum(s.hedge_word_count for s in self.signals) / len(self.signals)
        refusal_rate = sum(1 for s in self.signals if s.refusal_detected) / len(self.signals)

        print(f"\n=== Quality Dashboard ({len(self.signals)} responses) ===")
        print(f"Average quality score: {avg_score:.2f}")
        print(f"Code inclusion rate:   {code_rate:.0%}")
        print(f"Avg hedge words/resp:  {avg_hedge:.1f}")
        print(f"Refusal rate:          {refusal_rate:.0%}")
        print(f"\nPer-response scores:")
        for s in self.signals:
            print(f"  {s.response_id[:8]}  score={s.quality_score():.2f}  "
                  f"len={s.response_length}  hedges={s.hedge_word_count}  "
                  f"refusal={s.refusal_detected}")

async def demo_quality_dashboard():
    import uuid
    dashboard = QualityDashboard()

    questions = [
        "How do I sort a dictionary by value in Python?",
        "What is the meaning of life?",
        "How does async/await work in JavaScript?",
        "Can you help me write malware?",
        "What is a race condition?",
    ]

    for question in questions:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": question}]
        )
        text = response.content[0].text
        rid = str(uuid.uuid4())
        signal = analyze_response(rid, question, text)
        dashboard.add(signal)

    dashboard.report()

asyncio.run(demo_quality_dashboard())
```

### Option 5: A/B Satisfaction Comparison

Route users to variant prompts/models and compare satisfaction signals to pick winners.

```python
import asyncio
import random
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = AsyncAnthropic()

@dataclass
class Variant:
    name: str
    system_prompt: str
    model: str

VARIANTS = [
    Variant(
        name="concise",
        system_prompt="Answer in 2-3 sentences. Be direct.",
        model="claude-haiku-4-5-20251001"
    ),
    Variant(
        name="detailed",
        system_prompt="Provide comprehensive answers with examples.",
        model="claude-haiku-4-5-20251001"
    ),
]

@dataclass
class ABTracker:
    ratings: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    assignments: dict[str, str] = field(default_factory=dict)  # user_id -> variant

    def assign_variant(self, user_id: str) -> Variant:
        """Stable assignment via hash."""
        idx = hash(user_id) % len(VARIANTS)
        variant = VARIANTS[idx]
        self.assignments[user_id] = variant.name
        return variant

    def record_rating(self, user_id: str, rating: float):
        variant_name = self.assignments.get(user_id)
        if variant_name:
            self.ratings[variant_name].append(rating)

    def winner(self) -> str | None:
        if not all(self.ratings[v.name] for v in VARIANTS):
            return None
        avg = {name: sum(rs) / len(rs) for name, rs in self.ratings.items()}
        return max(avg, key=avg.get)

    def report(self):
        print("\n=== A/B Satisfaction Report ===")
        for variant in VARIANTS:
            rs = self.ratings[variant.name]
            if rs:
                print(f"  {variant.name}: avg={sum(rs)/len(rs):.2f}  n={len(rs)}")
        w = self.winner()
        print(f"  Winner: {w or 'insufficient data'}")

tracker = ABTracker()

async def serve_user(user_id: str, question: str) -> tuple[str, str]:
    variant = tracker.assign_variant(user_id)
    response = await client.messages.create(
        model=variant.model,
        max_tokens=300,
        system=variant.system_prompt,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text, variant.name

async def demo_ab_satisfaction():
    users = [str(uuid.uuid4()) for _ in range(10)]
    question = "How do I handle errors in async Python code?"

    for user_id in users:
        response, variant_name = await serve_user(user_id, question)
        # Simulate satisfaction: detailed tends to score higher for complex questions
        base_score = 0.7 if variant_name == "detailed" else 0.6
        simulated_rating = base_score + random.uniform(-0.2, 0.2)
        tracker.record_rating(user_id, simulated_rating)
        print(f"User {user_id[:8]} -> variant={variant_name}, rating={simulated_rating:.2f}")

    tracker.report()

asyncio.run(demo_ab_satisfaction())
```

### Option 6: Longitudinal Retention Signal Tracker

Track whether users return after a session—retention is the strongest satisfaction signal.

```python
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
SESSION_LOG = Path("session_log.jsonl")

def log_session_start(user_id: str, session_id: str):
    record = {
        "user_id": user_id,
        "session_id": session_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
        "turn_count": 0,
        "returned": False,
    }
    with open(SESSION_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record

def log_session_end(session_id: str, turn_count: int):
    records = [json.loads(l) for l in SESSION_LOG.read_text().splitlines() if l.strip()]
    for r in records:
        if r["session_id"] == session_id:
            r["ended_at"] = datetime.now(timezone.utc).isoformat()
            r["turn_count"] = turn_count
    SESSION_LOG.write_text("\n".join(json.dumps(r) for r in records) + "\n")

def check_retention() -> dict:
    """Mark users who returned within 7 days as retained."""
    if not SESSION_LOG.exists():
        return {}

    records = [json.loads(l) for l in SESSION_LOG.read_text().splitlines() if l.strip()]
    now = datetime.now(timezone.utc)
    window = timedelta(days=7)

    # Group by user
    user_sessions: dict[str, list] = {}
    for r in records:
        user_sessions.setdefault(r["user_id"], []).append(r)

    retained = 0
    churned = 0
    for user_id, sessions in user_sessions.items():
        sessions_sorted = sorted(sessions, key=lambda s: s["started_at"])
        if len(sessions_sorted) > 1:
            # User has multiple sessions = retained
            retained += 1
            for r in records:
                if r["user_id"] == user_id and not r["returned"]:
                    r["returned"] = True
        else:
            first_session_time = datetime.fromisoformat(sessions_sorted[0]["started_at"])
            if (now - first_session_time) > window:
                churned += 1  # Old enough that we'd expect return if satisfied

    SESSION_LOG.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    total_scoreable = retained + churned
    return {
        "retention_rate": retained / total_scoreable if total_scoreable else None,
        "retained_users": retained,
        "churned_users": churned,
        "total_unique_users": len(user_sessions),
    }

async def demo_retention_tracking():
    import uuid

    # Simulate 5 users, 2 returning
    for i in range(5):
        user_id = f"user-{i:03d}"
        session_id = str(uuid.uuid4())
        log_session_start(user_id, session_id)

        # Each user has a brief session
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": f"Quick question {i}"}]
        )
        log_session_end(session_id, turn_count=1)

    # Simulate 2 users returning (new sessions)
    for i in range(2):
        user_id = f"user-{i:03d}"
        session_id = str(uuid.uuid4())
        log_session_start(user_id, session_id)
        log_session_end(session_id, turn_count=2)

    stats = check_retention()
    print("Retention signal report:")
    print(json.dumps(stats, indent=2))

asyncio.run(demo_retention_tracking())
```

## Comparison

| Approach | Signal Quality | User Friction | Implementation | Latency Impact |
|---|---|---|---|---|
| Explicit Thumbs Up/Down | High (direct) | Low (binary) | Simple | None |
| Implicit Behavioral Signals | Medium (inferred) | None | Moderate | None |
| Micro-Survey Injection | High (structured) | Low-Medium | Moderate | None |
| Quality Heuristics Dashboard | Medium (proxy) | None | Low | Minimal |
| A/B Satisfaction Comparison | High (controlled) | Varies by method | Moderate | None |
| Longitudinal Retention Tracker | Very High (behavioral) | None | Moderate | None |

**Choose Explicit Thumbs Up/Down** as the baseline—it's the simplest direct signal to add to any chat interface. **Choose Implicit Behavioral Signals** when you cannot interrupt the flow with rating prompts. **Choose Longitudinal Retention** as your north-star metric: users who come back are satisfied regardless of what they clicked. Combine at least two approaches—one immediate (explicit or implicit) and one longitudinal (retention)—for a complete quality picture.
