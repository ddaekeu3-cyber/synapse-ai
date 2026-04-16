---
layout: solution
title: "Agent Doesn't Implement Automated Rollback on Quality Degradation"
category: reliability
description: "Detect quality degradation in live agents and automatically roll back to a previous model version, system prompt, or configuration before users are impacted."
tags: [rollback, quality-monitoring, reliability, production, canary, llm-judge]
---

## Problem

AI agents deployed in production silently degrade. A new system prompt increases hallucination rate by 15%. A model upgrade doubles response latency while cutting relevance. User satisfaction drops over 48 hours before anyone notices. Without automated quality gates and rollback, degradation compounds: bad outputs poison user trust, support tickets pile up, and engineering scrambles to diagnose root cause from stale logs.

```python
# Naive deployment: no quality monitoring whatsoever
def deploy_new_config(new_system_prompt: str):
    CONFIG["system_prompt"] = new_system_prompt  # hope for the best
    # no quality gate, no rollback, no monitoring
```

## Solution Options

### Option 1: Rolling Window Quality Score with Threshold Trigger

Track a rolling window of response quality scores. When the rolling average drops below a threshold, automatically revert to the last known-good configuration.

```python
import anthropic
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class QualityRecord:
    timestamp: float
    score: float          # 0.0 - 1.0
    config_version: str
    response_preview: str

@dataclass
class AgentConfig:
    version: str
    system_prompt: str
    model: str
    promoted_at: float = field(default_factory=time.time)

class RollingQualityGuard:
    def __init__(
        self,
        window_size: int = 20,
        rollback_threshold: float = 0.65,
        min_samples_before_rollback: int = 5,
    ):
        self.window_size = window_size
        self.rollback_threshold = rollback_threshold
        self.min_samples = min_samples_before_rollback
        self.window: deque[QualityRecord] = deque(maxlen=window_size)
        self.configs: list[AgentConfig] = []
        self.active_idx: int = 0
        self.rollback_count: int = 0
        self.client = anthropic.Anthropic()

    def push_config(self, config: AgentConfig) -> None:
        self.configs.append(config)
        self.active_idx = len(self.configs) - 1

    @property
    def active_config(self) -> AgentConfig:
        return self.configs[self.active_idx]

    def _score_response(self, user_message: str, response: str) -> float:
        """Heuristic quality score: length, refusal detection, coherence signals."""
        score = 1.0
        if len(response) < 20:
            score -= 0.4
        refusal_phrases = ["i cannot", "i'm unable", "as an ai", "i don't have access"]
        if any(p in response.lower() for p in refusal_phrases):
            score -= 0.3
        if response.count("?") > 5:
            score -= 0.1
        repetition = len(set(response.split())) / max(len(response.split()), 1)
        if repetition < 0.4:
            score -= 0.2
        return max(0.0, min(1.0, score))

    def _rolling_average(self) -> Optional[float]:
        if not self.window:
            return None
        return sum(r.score for r in self.window) / len(self.window)

    def _should_rollback(self) -> bool:
        if len(self.window) < self.min_samples:
            return False
        avg = self._rolling_average()
        return avg is not None and avg < self.rollback_threshold

    def rollback(self) -> bool:
        if self.active_idx == 0:
            print("Already at oldest config, cannot rollback further.")
            return False
        prev_version = self.active_config.version
        self.active_idx -= 1
        self.rollback_count += 1
        self.window.clear()  # reset window after rollback
        print(
            f"[ROLLBACK] {prev_version} → {self.active_config.version} "
            f"(rollback #{self.rollback_count})"
        )
        return True

    def respond(self, user_message: str) -> str:
        cfg = self.active_config
        resp = self.client.messages.create(
            model=cfg.model,
            max_tokens=512,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = resp.content[0].text
        score = self._score_response(user_message, text)
        record = QualityRecord(
            timestamp=time.time(),
            score=score,
            config_version=cfg.version,
            response_preview=text[:80],
        )
        self.window.append(record)
        avg = self._rolling_average()
        print(f"[QUALITY] score={score:.2f} rolling_avg={avg:.2f} config={cfg.version}")
        if self._should_rollback():
            self.rollback()
        return text


# Usage
guard = RollingQualityGuard(window_size=20, rollback_threshold=0.65)
guard.push_config(AgentConfig("v1.0", "You are a helpful assistant.", "claude-haiku-4-5-20251001"))
guard.push_config(AgentConfig("v2.0", "You are concise.", "claude-haiku-4-5-20251001"))

for q in ["What is Python?", "Explain recursion", "What is async/await?"]:
    answer = guard.respond(q)
    print(f"Answer: {answer[:100]}\n")

# Expected Token Savings: N/A — quality monitoring adds ~0 tokens; prevents costly bad-output incidents
# Environment: ANTHROPIC_API_KEY
```

### Option 2: LLM-Judge Quality Monitoring

Use a fast judge model (Haiku) to evaluate every Nth response against a rubric. Rollback when judge scores trend below threshold.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

@dataclass
class JudgeVerdict:
    relevance: float       # 0–1
    accuracy: float        # 0–1
    completeness: float    # 0–1
    overall: float         # 0–1
    reasoning: str

@dataclass
class AgentConfig:
    version: str
    system_prompt: str
    model: str

class LLMJudgeRollbackGuard:
    JUDGE_PROMPT = """Evaluate this AI response on a scale of 0.0–1.0 for each dimension.

User question: {question}
AI response: {response}

Return JSON only:
{{
  "relevance": <float>,
  "accuracy": <float>,
  "completeness": <float>,
  "overall": <float>,
  "reasoning": "<one sentence>"
}}"""

    def __init__(
        self,
        judge_every_n: int = 3,
        window_size: int = 10,
        rollback_threshold: float = 0.60,
    ):
        self.judge_every_n = judge_every_n
        self.window_size = window_size
        self.rollback_threshold = rollback_threshold
        self.configs: list[AgentConfig] = []
        self.active_idx: int = 0
        self.call_count: int = 0
        self.verdicts: deque[JudgeVerdict] = deque(maxlen=window_size)
        self.client = anthropic.Anthropic()

    def push_config(self, config: AgentConfig) -> None:
        self.configs.append(config)
        self.active_idx = len(self.configs) - 1

    @property
    def active(self) -> AgentConfig:
        return self.configs[self.active_idx]

    def _judge(self, question: str, response: str) -> Optional[JudgeVerdict]:
        prompt = self.JUDGE_PROMPT.format(question=question, response=response)
        try:
            r = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            data = json.loads(r.content[0].text)
            return JudgeVerdict(**data)
        except Exception as e:
            print(f"[JUDGE ERROR] {e}")
            return None

    def _rolling_average(self) -> Optional[float]:
        if not self.verdicts:
            return None
        return sum(v.overall for v in self.verdicts) / len(self.verdicts)

    def _rollback(self) -> bool:
        if self.active_idx == 0:
            return False
        prev = self.active.version
        self.active_idx -= 1
        self.verdicts.clear()
        print(f"[ROLLBACK] {prev} → {self.active.version}")
        return True

    def respond(self, question: str) -> str:
        cfg = self.active
        r = self.client.messages.create(
            model=cfg.model,
            max_tokens=512,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        text = r.content[0].text
        self.call_count += 1

        if self.call_count % self.judge_every_n == 0:
            verdict = self._judge(question, text)
            if verdict:
                self.verdicts.append(verdict)
                avg = self._rolling_average()
                print(
                    f"[JUDGE] overall={verdict.overall:.2f} rolling={avg:.2f} "
                    f"reason={verdict.reasoning}"
                )
                if avg is not None and avg < self.rollback_threshold:
                    self._rollback()
        return text


# Usage
guard = LLMJudgeRollbackGuard(judge_every_n=2, rollback_threshold=0.60)
guard.push_config(AgentConfig("v1.0", "You are a precise technical assistant.", "claude-haiku-4-5-20251001"))
guard.push_config(AgentConfig("v2.0-experimental", "Be brief.", "claude-haiku-4-5-20251001"))

questions = [
    "Explain Python decorators",
    "What is a closure?",
    "Describe the GIL",
    "What is metaclass?",
]
for q in questions:
    ans = guard.respond(q)
    print(f"Q: {q}\nA: {ans[:120]}\n")

# Expected Token Savings: Judge adds ~150 tokens per evaluation (every 3rd call); prevents quality drift
# Environment: ANTHROPIC_API_KEY
```

### Option 3: User Feedback Signal Integration

Collect explicit thumbs-up/thumbs-down signals. Weight recent feedback with exponential decay. Trigger rollback when satisfaction drops below threshold.

```python
import anthropic
import time
import math
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class FeedbackEvent:
    timestamp: float
    thumbs_up: bool
    config_version: str
    message_id: str

@dataclass
class AgentConfig:
    version: str
    system_prompt: str
    model: str

class FeedbackDrivenRollback:
    def __init__(
        self,
        decay_halflife_seconds: float = 3600.0,  # 1 hour
        rollback_threshold: float = 0.45,
        min_feedback_events: int = 5,
    ):
        self.decay_halflife = decay_halflife_seconds
        self.rollback_threshold = rollback_threshold
        self.min_events = min_feedback_events
        self.configs: list[AgentConfig] = []
        self.active_idx: int = 0
        self.feedback_log: list[FeedbackEvent] = []
        self.message_counter: int = 0
        self.client = anthropic.Anthropic()

    def push_config(self, config: AgentConfig) -> None:
        self.configs.append(config)
        self.active_idx = len(self.configs) - 1
        self.feedback_log.clear()  # fresh slate for new config

    @property
    def active(self) -> AgentConfig:
        return self.configs[self.active_idx]

    def record_feedback(self, message_id: str, thumbs_up: bool) -> None:
        event = FeedbackEvent(
            timestamp=time.time(),
            thumbs_up=thumbs_up,
            config_version=self.active.version,
            message_id=message_id,
        )
        self.feedback_log.append(event)
        score = self._weighted_satisfaction()
        n = len(self.feedback_log)
        print(f"[FEEDBACK] {'👍' if thumbs_up else '👎'} weighted_sat={score:.2f} n={n}")
        if n >= self.min_events and score < self.rollback_threshold:
            self._rollback()

    def _weighted_satisfaction(self) -> float:
        now = time.time()
        total_weight = 0.0
        positive_weight = 0.0
        for event in self.feedback_log:
            age = now - event.timestamp
            weight = math.exp(-age * math.log(2) / self.decay_halflife)
            total_weight += weight
            if event.thumbs_up:
                positive_weight += weight
        if total_weight == 0:
            return 1.0
        return positive_weight / total_weight

    def _rollback(self) -> bool:
        if self.active_idx == 0:
            print("[ROLLBACK] Already at baseline config")
            return False
        prev = self.active.version
        self.active_idx -= 1
        self.feedback_log.clear()
        print(f"[ROLLBACK] User feedback triggered: {prev} → {self.active.version}")
        return True

    def respond(self, user_message: str) -> tuple[str, str]:
        """Returns (response_text, message_id) for later feedback."""
        cfg = self.active
        self.message_counter += 1
        message_id = f"msg_{self.message_counter}"
        r = self.client.messages.create(
            model=cfg.model,
            max_tokens=512,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return r.content[0].text, message_id


# Simulate production feedback loop
guard = FeedbackDrivenRollback(rollback_threshold=0.45, min_feedback_events=3)
guard.push_config(AgentConfig("v1.0-stable", "You are a helpful assistant.", "claude-haiku-4-5-20251001"))
guard.push_config(AgentConfig("v2.0-experimental", "Be extremely brief, one sentence max.", "claude-haiku-4-5-20251001"))

# Simulate conversation + feedback
conversations = [
    ("How do I read a file in Python?", False),   # thumbs down — too brief
    ("What is a list comprehension?", False),      # thumbs down
    ("What does async mean?", False),              # thumbs down — triggers rollback
    ("Explain generators", True),                  # after rollback, better response
]

for question, simulated_feedback in conversations:
    text, msg_id = guard.respond(question)
    print(f"Config: {guard.active.version}")
    print(f"A: {text[:120]}")
    guard.record_feedback(msg_id, simulated_feedback)
    print()

# Expected Token Savings: Feedback integration is zero-token overhead; prevents prolonged quality incidents
# Environment: ANTHROPIC_API_KEY
```

### Option 4: A/B Quality Comparison with Automatic Revert

Simultaneously run both the candidate and baseline configs on a traffic sample. Compare quality scores. Revert candidate to baseline if it consistently underperforms.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ABResult:
    question: str
    baseline_response: str
    candidate_response: str
    baseline_score: float
    candidate_score: float
    winner: str  # "baseline" | "candidate" | "tie"

@dataclass
class AgentConfig:
    name: str
    system_prompt: str
    model: str

class ABQualityComparator:
    COMPARE_PROMPT = """You are a quality judge. Compare these two AI responses to the same question.

Question: {question}

Response A: {response_a}

Response B: {response_b}

Rate each from 0.0–1.0. Return JSON only:
{{
  "score_a": <float>,
  "score_b": <float>,
  "winner": "A" | "B" | "tie",
  "reasoning": "<one sentence>"
}}"""

    def __init__(
        self,
        sample_rate: float = 0.3,
        window_size: int = 10,
        revert_threshold: float = 0.6,  # candidate wins < 60% → revert
    ):
        self.sample_rate = sample_rate
        self.window_size = window_size
        self.revert_threshold = revert_threshold
        self.results: list[ABResult] = []
        self.reverted: bool = False
        self.client = anthropic.AsyncAnthropic()

    async def _generate(self, config: AgentConfig, question: str) -> str:
        r = await self.client.messages.create(
            model=config.model,
            max_tokens=512,
            system=config.system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        return r.content[0].text

    async def _compare(self, question: str, resp_a: str, resp_b: str) -> dict:
        prompt = self.COMPARE_PROMPT.format(
            question=question, response_a=resp_a, response_b=resp_b
        )
        r = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(r.content[0].text)

    def _candidate_win_rate(self) -> float:
        if not self.results:
            return 1.0
        wins = sum(1 for r in self.results[-self.window_size:] if r.winner == "candidate")
        return wins / len(self.results[-self.window_size:])

    async def evaluate(
        self,
        baseline: AgentConfig,
        candidate: AgentConfig,
        question: str,
    ) -> tuple[str, bool]:
        """Returns (production_response, reverted)."""
        import random
        if self.reverted or random.random() > self.sample_rate:
            # Not sampling — serve candidate directly
            return await self._generate(candidate if not self.reverted else baseline, question), self.reverted

        # Run both in parallel
        baseline_resp, candidate_resp = await asyncio.gather(
            self._generate(baseline, question),
            self._generate(candidate, question),
        )
        verdict = await self._compare(question, baseline_resp, candidate_resp)
        winner_label = "baseline" if verdict["winner"] == "A" else (
            "candidate" if verdict["winner"] == "B" else "tie"
        )
        result = ABResult(
            question=question,
            baseline_response=baseline_resp,
            candidate_response=candidate_resp,
            baseline_score=verdict["score_a"],
            candidate_score=verdict["score_b"],
            winner=winner_label,
        )
        self.results.append(result)
        win_rate = self._candidate_win_rate()
        print(
            f"[A/B] baseline={result.baseline_score:.2f} candidate={result.candidate_score:.2f} "
            f"win_rate={win_rate:.2f} reason={verdict['reasoning']}"
        )
        if len(self.results) >= 5 and win_rate < self.revert_threshold:
            print(f"[REVERT] Candidate win rate {win_rate:.2f} < {self.revert_threshold} — reverting to baseline")
            self.reverted = True
        # Serve candidate response (or baseline if reverted)
        return (baseline_resp if self.reverted else candidate_resp), self.reverted


async def main():
    baseline = AgentConfig("v1.0", "You are a thorough technical assistant.", "claude-haiku-4-5-20251001")
    candidate = AgentConfig("v2.0", "You are extremely brief.", "claude-haiku-4-5-20251001")
    comparator = ABQualityComparator(sample_rate=1.0, revert_threshold=0.6)

    questions = [
        "Explain Python type hints",
        "What is dependency injection?",
        "Describe the observer pattern",
        "What are dataclasses?",
        "Explain context managers",
        "What is duck typing?",
    ]
    for q in questions:
        response, reverted = await comparator.evaluate(baseline, candidate, q)
        status = "REVERTED" if reverted else "candidate"
        print(f"[{status}] {q[:40]}: {response[:80]}\n")

asyncio.run(main())

# Expected Token Savings: A/B sampling adds judge tokens; saves from deploying low-quality configs broadly
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Multi-Metric Composite Score with Rollback

Combine multiple quality signals (relevance, response time, refusal rate, length appropriateness) into a weighted composite score. Rollback when composite degrades.

```python
import anthropic
import time
import json
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

@dataclass
class MetricSnapshot:
    timestamp: float
    relevance_score: float    # LLM judge, 0–1
    latency_ms: float
    refusal: bool
    length_score: float       # penalize too short or too long
    composite: float

@dataclass
class AgentConfig:
    version: str
    system_prompt: str
    model: str
    ideal_response_length: int = 300  # chars

class CompositeQualityGuard:
    RELEVANCE_PROMPT = """Rate the relevance of this response to the question.
Question: {question}
Response: {response}
Return JSON: {{"relevance": <0.0-1.0>, "refusal": <true|false>}}"""

    WEIGHTS = {
        "relevance": 0.45,
        "latency": 0.20,
        "refusal": 0.20,
        "length": 0.15,
    }

    def __init__(
        self,
        window_size: int = 15,
        rollback_threshold: float = 0.60,
        min_samples: int = 5,
        max_latency_ms: float = 5000.0,
    ):
        self.window_size = window_size
        self.rollback_threshold = rollback_threshold
        self.min_samples = min_samples
        self.max_latency_ms = max_latency_ms
        self.window: deque[MetricSnapshot] = deque(maxlen=window_size)
        self.configs: list[AgentConfig] = []
        self.active_idx: int = 0
        self.client = anthropic.Anthropic()

    def push_config(self, config: AgentConfig) -> None:
        self.configs.append(config)
        self.active_idx = len(self.configs) - 1
        self.window.clear()

    @property
    def active(self) -> AgentConfig:
        return self.configs[self.active_idx]

    def _relevance_score(self, question: str, response: str) -> tuple[float, bool]:
        prompt = self.RELEVANCE_PROMPT.format(question=question, response=response)
        try:
            r = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            data = json.loads(r.content[0].text)
            return float(data["relevance"]), bool(data["refusal"])
        except Exception:
            return 0.5, False

    def _length_score(self, response: str, ideal: int) -> float:
        length = len(response)
        ratio = length / ideal
        if 0.5 <= ratio <= 2.0:
            return 1.0
        elif ratio < 0.5:
            return ratio / 0.5
        else:
            return max(0.0, 1.0 - (ratio - 2.0) * 0.2)

    def _compute_composite(self, snap: MetricSnapshot) -> float:
        latency_score = max(0.0, 1.0 - snap.latency_ms / self.max_latency_ms)
        refusal_score = 0.0 if snap.refusal else 1.0
        return (
            self.WEIGHTS["relevance"] * snap.relevance_score
            + self.WEIGHTS["latency"] * latency_score
            + self.WEIGHTS["refusal"] * refusal_score
            + self.WEIGHTS["length"] * snap.length_score
        )

    def _rolling_composite(self) -> Optional[float]:
        if not self.window:
            return None
        return sum(s.composite for s in self.window) / len(self.window)

    def _rollback(self) -> bool:
        if self.active_idx == 0:
            print("[ROLLBACK] At baseline, cannot revert further")
            return False
        prev = self.active.version
        self.active_idx -= 1
        self.window.clear()
        print(f"[ROLLBACK] {prev} → {self.active.version}")
        return True

    def respond(self, question: str) -> str:
        cfg = self.active
        t0 = time.monotonic()
        r = self.client.messages.create(
            model=cfg.model,
            max_tokens=512,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        latency_ms = (time.monotonic() - t0) * 1000
        text = r.content[0].text
        relevance, refusal = self._relevance_score(question, text)
        length_score = self._length_score(text, cfg.ideal_response_length)
        snap = MetricSnapshot(
            timestamp=time.monotonic(),
            relevance_score=relevance,
            latency_ms=latency_ms,
            refusal=refusal,
            length_score=length_score,
            composite=0.0,
        )
        snap.composite = self._compute_composite(snap)
        self.window.append(snap)
        avg = self._rolling_composite()
        print(
            f"[METRICS] rel={relevance:.2f} lat={latency_ms:.0f}ms "
            f"ref={refusal} len={length_score:.2f} composite={snap.composite:.2f} avg={avg:.2f}"
        )
        if len(self.window) >= self.min_samples and avg is not None and avg < self.rollback_threshold:
            self._rollback()
        return text


# Usage
guard = CompositeQualityGuard(window_size=15, rollback_threshold=0.60)
guard.push_config(AgentConfig("v1.0", "You are a helpful assistant.", "claude-haiku-4-5-20251001"))
guard.push_config(AgentConfig("v2.0", "Respond in one word only.", "claude-haiku-4-5-20251001"))

for q in ["What is Python?", "Explain async programming", "What is recursion?"]:
    ans = guard.respond(q)
    print(f"Q: {q}\nA: {ans[:100]}\n")

# Expected Token Savings: Judge calls ~64 tokens each; composite scoring prevents quality incidents worth thousands in support cost
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Canary Deployment with Staged Rollback

Route a small percentage of traffic to the new config. Monitor quality in real time. Expand traffic share if quality holds; roll back completely if it drops.

```python
import anthropic
import asyncio
import random
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

@dataclass
class CanaryResult:
    timestamp: float
    config_name: str
    score: float

@dataclass
class AgentConfig:
    name: str
    system_prompt: str
    model: str

class CanaryDeploymentGuard:
    STAGES = [0.05, 0.10, 0.25, 0.50, 1.00]  # traffic share progression

    def __init__(
        self,
        promote_threshold: float = 0.72,
        rollback_threshold: float = 0.55,
        stage_window: int = 10,       # samples needed to evaluate a stage
        min_stage_samples: int = 5,
    ):
        self.promote_threshold = promote_threshold
        self.rollback_threshold = rollback_threshold
        self.stage_window = stage_window
        self.min_stage_samples = min_stage_samples
        self.stage_idx: int = 0
        self.baseline: Optional[AgentConfig] = None
        self.candidate: Optional[AgentConfig] = None
        self.canary_results: deque[CanaryResult] = deque(maxlen=stage_window)
        self.fully_rolled_back: bool = False
        self.fully_promoted: bool = False
        self.client = anthropic.AsyncAnthropic()

    def deploy_canary(self, baseline: AgentConfig, candidate: AgentConfig) -> None:
        self.baseline = baseline
        self.candidate = candidate
        self.stage_idx = 0
        self.canary_results.clear()
        self.fully_rolled_back = False
        self.fully_promoted = False
        traffic = self.STAGES[self.stage_idx]
        print(f"[CANARY] Deploying {candidate.name} at {traffic*100:.0f}% traffic")

    def _heuristic_score(self, response: str) -> float:
        score = 1.0
        if len(response) < 30:
            score -= 0.4
        if len(response) > 2000:
            score -= 0.2
        refusals = ["i cannot", "i'm unable", "as an ai"]
        if any(p in response.lower() for p in refusals):
            score -= 0.35
        word_count = len(response.split())
        unique_ratio = len(set(response.lower().split())) / max(word_count, 1)
        if unique_ratio < 0.4:
            score -= 0.15
        return max(0.0, min(1.0, score))

    def _canary_average(self) -> Optional[float]:
        if not self.canary_results:
            return None
        return sum(r.score for r in self.canary_results) / len(self.canary_results)

    def _evaluate_stage(self) -> None:
        if len(self.canary_results) < self.min_stage_samples:
            return
        avg = self._canary_average()
        traffic = self.STAGES[self.stage_idx]
        print(f"[CANARY] Stage {self.stage_idx+1}/{len(self.STAGES)} "
              f"traffic={traffic*100:.0f}% avg_score={avg:.2f}")
        if avg < self.rollback_threshold:
            print(f"[CANARY ROLLBACK] Score {avg:.2f} < {self.rollback_threshold} — full rollback")
            self.fully_rolled_back = True
            return
        if avg >= self.promote_threshold:
            if self.stage_idx < len(self.STAGES) - 1:
                self.stage_idx += 1
                self.canary_results.clear()
                new_traffic = self.STAGES[self.stage_idx]
                print(f"[CANARY PROMOTE] Advancing to {new_traffic*100:.0f}% traffic")
                if self.stage_idx == len(self.STAGES) - 1:
                    print(f"[CANARY COMPLETE] {self.candidate.name} fully promoted")
                    self.fully_promoted = True

    async def respond(self, question: str) -> str:
        if self.fully_rolled_back or self.candidate is None:
            cfg = self.baseline
            label = "baseline"
        elif self.fully_promoted:
            cfg = self.candidate
            label = "candidate(100%)"
        else:
            traffic = self.STAGES[self.stage_idx]
            if random.random() < traffic:
                cfg = self.candidate
                label = f"canary({traffic*100:.0f}%)"
            else:
                cfg = self.baseline
                label = "baseline"

        r = await self.client.messages.create(
            model=cfg.model,
            max_tokens=512,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        text = r.content[0].text

        # Track canary results only for candidate traffic
        if label.startswith("canary"):
            score = self._heuristic_score(text)
            self.canary_results.append(
                CanaryResult(timestamp=time.time(), config_name=cfg.name, score=score)
            )
            self._evaluate_stage()
        return text


async def main():
    guard = CanaryDeploymentGuard(
        promote_threshold=0.72,
        rollback_threshold=0.55,
        stage_window=5,
        min_stage_samples=3,
    )
    baseline = AgentConfig("v1.0-stable", "You are a helpful technical assistant.", "claude-haiku-4-5-20251001")
    candidate = AgentConfig("v2.0-canary", "You are a precise technical assistant.", "claude-haiku-4-5-20251001")
    guard.deploy_canary(baseline, candidate)

    questions = [
        "What is Python?",
        "Explain list comprehensions",
        "What is a generator?",
        "What are decorators?",
        "Explain asyncio",
        "What is type hinting?",
        "Describe dataclasses",
        "What is a context manager?",
        "Explain slots in Python",
        "What is the GIL?",
    ]
    tasks = [guard.respond(q) for q in questions]
    responses = await asyncio.gather(*tasks)
    for q, r in zip(questions, responses):
        print(f"Q: {q}\nA: {r[:100]}\n")

asyncio.run(main())

# Expected Token Savings: Canary limits blast radius; scoring heuristic is zero-token; prevents mass rollout of degraded configs
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Detection Mechanism | Rollback Trigger | Overhead | Best For |
|--------|--------------------|--------------------|----------|----------|
| 1. Rolling Window | Heuristic score avg | Below threshold | Zero tokens | Fast heuristic monitoring |
| 2. LLM Judge | Haiku judge every Nth | Avg score < threshold | ~150 tok/eval | High-accuracy evaluation |
| 3. User Feedback | Exponential decay CSAT | Weighted satisfaction | Zero tokens | User-facing products |
| 4. A/B Comparison | Parallel judge scoring | Win rate < threshold | 2× tokens for sample | Evaluating experimental configs |
| 5. Composite Score | Relevance + latency + refusal + length | Weighted composite < threshold | ~64 tok/eval | Multi-dimensional quality |
| 6. Canary Staged | Heuristic on traffic slice | Stage avg < threshold | Zero tokens | Risk-averse production rollout |

**Recommended approach**: Combine Option 6 (canary) for initial deployment risk management with Option 2 (LLM judge) for ongoing monitoring after full promotion.
