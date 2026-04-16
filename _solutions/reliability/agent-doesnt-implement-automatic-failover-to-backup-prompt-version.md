---
title: "Agent Doesn't Implement Automatic Failover to Backup Prompt Version"
description: "Six solutions for automatically falling back to known-good prompt versions when quality degrades, using circuit breakers, LLM judges, and versioned prompt stores."
difficulty: intermediate
category: reliability
tags: [prompts, failover, circuit-breaker, quality, versioning, rollback]
---

# Agent Doesn't Implement Automatic Failover to Backup Prompt Version

Prompt changes that degrade output quality may go undetected for hours. Without an automatic fallback mechanism, a bad prompt ships to all users until someone notices manually. These six solutions instrument prompt quality and switch to backup versions the moment signals indicate degradation.

## Solution 1: EMA Quality Score with Automatic Rollback

Track an exponential moving average of quality scores; roll back to the backup prompt when EMA drops below threshold.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from anthropic import AsyncAnthropic


class PromptStatus(Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    ROLLED_BACK = "rolled_back"


@dataclass
class PromptVersion:
    version: str
    system_prompt: str
    created_at: float = field(default_factory=time.time)
    is_backup: bool = False


@dataclass
class QualityTracker:
    ema_alpha: float = 0.1          # Smoothing factor (lower = slower to react)
    rollback_threshold: float = 0.6  # EMA below this triggers rollback
    min_samples: int = 5             # Minimum samples before acting

    ema: float = 1.0
    sample_count: int = 0
    status: PromptStatus = PromptStatus.ACTIVE

    def update(self, score: float) -> bool:
        """Returns True if rollback should be triggered."""
        self.sample_count += 1
        self.ema = self.ema_alpha * score + (1 - self.ema_alpha) * self.ema
        if self.sample_count >= self.min_samples and self.ema < self.rollback_threshold:
            self.status = PromptStatus.DEGRADED
            return True
        return False


class EMAFailoverAgent:
    def __init__(self, active: PromptVersion, backup: PromptVersion):
        self.client = AsyncAnthropic()
        self.active = active
        self.backup = backup
        self.current = active
        self.tracker = QualityTracker()
        self._rollback_callbacks: list = []

    def on_rollback(self, callback):
        self._rollback_callbacks.append(callback)
        return callback

    def _rollback(self):
        print(
            f"[ROLLBACK] EMA={self.tracker.ema:.3f} < threshold={self.tracker.rollback_threshold}. "
            f"Switching {self.active.version} -> {self.backup.version}"
        )
        self.current = self.backup
        self.tracker.status = PromptStatus.ROLLED_BACK
        for cb in self._rollback_callbacks:
            cb(self.active, self.backup, self.tracker.ema)

    async def _score_response(self, user_message: str, response: str) -> float:
        """LLM-as-judge: returns 0.0–1.0 quality score."""
        judge = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": (
                    f"Rate this response quality 0-10 (integer only).\n"
                    f"User: {user_message}\nResponse: {response}"
                ),
            }],
        )
        try:
            raw = judge.content[0].text.strip().split()[0]
            return min(10, max(0, int(raw))) / 10.0
        except (ValueError, IndexError):
            return 0.5

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            system=self.current.system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text

        # Only score and potentially roll back while on the active prompt
        if self.current is self.active and self.tracker.status == PromptStatus.ACTIVE:
            score = await self._score_response(message, text)
            should_rollback = self.tracker.update(score)
            if should_rollback:
                self._rollback()

        return text


# Usage
async def demo_ema_failover():
    active = PromptVersion(
        version="v2.0",
        system_prompt="You are a helpful assistant. Always respond in exactly one word.",
    )
    backup = PromptVersion(
        version="v1.9",
        system_prompt="You are a helpful, detailed assistant.",
        is_backup=True,
    )
    agent = EMAFailoverAgent(active, backup)

    @agent.on_rollback
    def notify(from_version, to_version, ema):
        print(f"Alert: rolled back from {from_version.version} to {to_version.version} (EMA={ema:.3f})")

    messages = [
        "Explain the water cycle in detail.",
        "What causes earthquakes?",
        "How does photosynthesis work?",
        "Describe the history of computing.",
    ]
    for msg in messages:
        result = await agent.chat(msg)
        print(f"[{agent.current.version}] {result[:80]}")
```

## Solution 2: Circuit Breaker on Prompt Version

Open the circuit after N consecutive poor-quality responses; immediately fall back to backup until a probe succeeds.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from anthropic import AsyncAnthropic


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation — current prompt
    OPEN = "open"           # Fault — using backup prompt
    HALF_OPEN = "half_open" # Probing — trying current again


@dataclass
class PromptCircuitBreaker:
    failure_threshold: int = 3       # Consecutive failures before opening
    success_threshold: int = 2       # Consecutive successes before closing from half-open
    recovery_timeout: float = 120.0  # Seconds before trying half-open

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: float = 0.0

    def record_success(self):
        self.consecutive_failures = 0
        if self.state == CircuitState.HALF_OPEN:
            self.consecutive_successes += 1
            if self.consecutive_successes >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.consecutive_successes = 0
                print("[CIRCUIT] Closed — current prompt restored")

    def record_failure(self):
        self.consecutive_successes = 0
        self.consecutive_failures += 1
        if (
            self.state == CircuitState.CLOSED
            and self.consecutive_failures >= self.failure_threshold
        ) or self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            print(f"[CIRCUIT] Opened after {self.consecutive_failures} failures — using backup")

    def should_use_backup(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return False
        if self.state == CircuitState.OPEN:
            if time.time() - self.opened_at >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                print("[CIRCUIT] Half-open — probing current prompt")
                return False  # Probe with current
            return True
        return False  # HALF_OPEN: try current


@dataclass
class VersionedPrompt:
    version: str
    content: str
    quality_min: float = 0.65  # Scores below this count as failure


class CircuitBreakerAgent:
    def __init__(self, current: VersionedPrompt, backup: VersionedPrompt):
        self.client = AsyncAnthropic()
        self.current_prompt = current
        self.backup_prompt = backup
        self.breaker = PromptCircuitBreaker()

    async def _judge_quality(self, user_msg: str, response: str) -> float:
        judge = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{
                "role": "user",
                "content": (
                    f"Score this response 0-10 (integer only).\n"
                    f"Q: {user_msg}\nA: {response}"
                ),
            }],
        )
        try:
            return int(judge.content[0].text.strip().split()[0]) / 10.0
        except (ValueError, IndexError):
            return 0.5

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        use_backup = self.breaker.should_use_backup()
        prompt = self.backup_prompt if use_backup else self.current_prompt
        print(f"[{self.breaker.state.value}] Using prompt {prompt.version}")

        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            system=prompt.content,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text

        # Only evaluate quality when using current (not backup)
        if not use_backup:
            score = await self._judge_quality(message, text)
            if score < self.current_prompt.quality_min:
                self.breaker.record_failure()
            else:
                self.breaker.record_success()

        return text
```

## Solution 3: Shadow Testing Before Promotion

Run both current and backup prompts in shadow; promote only when current outperforms backup consistently.

```python
import asyncio
import statistics
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ShadowTestResult:
    current_score: float
    shadow_score: float
    winner: str  # "current" | "shadow" | "tie"


@dataclass
class PromptCandidate:
    name: str
    system_prompt: str
    scores: list[float] = field(default_factory=list)

    @property
    def mean_score(self) -> float:
        return statistics.mean(self.scores) if self.scores else 0.0

    @property
    def sample_count(self) -> int:
        return len(self.scores)


class ShadowTestingAgent:
    """
    Runs shadow tests on every Nth request.
    Current prompt is used for the user response.
    Shadow (backup) prompt is evaluated silently.
    If shadow consistently wins, trigger failover alert.
    """

    def __init__(
        self,
        current: PromptCandidate,
        shadow: PromptCandidate,
        shadow_rate: float = 0.2,
        min_samples: int = 10,
        shadow_win_threshold: float = 0.05,  # Shadow must beat current by this margin
    ):
        self.client = AsyncAnthropic()
        self.current = current
        self.shadow = shadow
        self.shadow_rate = shadow_rate
        self.min_samples = min_samples
        self.shadow_win_threshold = shadow_win_threshold
        self._shadow_wins = 0
        self._total_compared = 0

    async def _score(self, user_msg: str, response: str) -> float:
        judge = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{
                "role": "user",
                "content": f"Rate 0-10 (integer): Q={user_msg[:100]} A={response[:200]}",
            }],
        )
        try:
            return int(judge.content[0].text.strip().split()[0]) / 10.0
        except (ValueError, IndexError):
            return 0.5

    async def _run_shadow(self, message: str, model: str) -> ShadowTestResult:
        """Run both prompts concurrently; score both."""
        current_resp, shadow_resp = await asyncio.gather(
            self.client.messages.create(
                model=model,
                max_tokens=1024,
                system=self.current.system_prompt,
                messages=[{"role": "user", "content": message}],
            ),
            self.client.messages.create(
                model=model,
                max_tokens=1024,
                system=self.shadow.system_prompt,
                messages=[{"role": "user", "content": message}],
            ),
        )
        current_score, shadow_score = await asyncio.gather(
            self._score(message, current_resp.content[0].text),
            self._score(message, shadow_resp.content[0].text),
        )
        self.current.scores.append(current_score)
        self.shadow.scores.append(shadow_score)
        self._total_compared += 1
        if shadow_score > current_score + self.shadow_win_threshold:
            self._shadow_wins += 1

        winner = (
            "shadow" if shadow_score > current_score + self.shadow_win_threshold
            else "current" if current_score > shadow_score + self.shadow_win_threshold
            else "tie"
        )
        return ShadowTestResult(current_score, shadow_score, winner)

    def should_failover(self) -> bool:
        if self._total_compared < self.min_samples:
            return False
        shadow_win_rate = self._shadow_wins / self._total_compared
        return shadow_win_rate > 0.6  # Shadow wins >60% → recommend failover

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        import random
        run_shadow = random.random() < self.shadow_rate

        if run_shadow:
            result = await self._run_shadow(message, model)
            print(
                f"[SHADOW] current={result.current_score:.2f} "
                f"shadow={result.shadow_score:.2f} winner={result.winner}"
            )
            if self.should_failover():
                print(
                    f"[ALERT] Shadow prompt outperforms current "
                    f"({self._shadow_wins}/{self._total_compared} wins). Consider failover."
                )
        # Always serve from current prompt
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            system=self.current.system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text
```

## Solution 4: Versioned Prompt Store with Health-Check-Driven Selection

Maintain a prompt store with health scores; always select the highest-health version at request time.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class PromptRecord:
    version: str
    content: str
    health_score: float = 1.0       # 0.0–1.0; decays on failures
    request_count: int = 0
    error_count: int = 0
    created_at: float = field(default_factory=time.time)
    pinned: bool = False             # Pinned versions are never auto-selected

    @property
    def error_rate(self) -> float:
        return self.error_count / max(self.request_count, 1)

    def decay_health(self, amount: float = 0.1):
        self.health_score = max(0.0, self.health_score - amount)

    def recover_health(self, amount: float = 0.02):
        self.health_score = min(1.0, self.health_score + amount)


class PromptStore:
    def __init__(self):
        self._versions: dict[str, PromptRecord] = {}
        self._active_version: str | None = None

    def register(self, record: PromptRecord, set_active: bool = False):
        self._versions[record.version] = record
        if set_active or self._active_version is None:
            self._active_version = record.version

    def best_healthy_version(self, min_health: float = 0.5) -> PromptRecord:
        """Return highest-health non-pinned version above threshold."""
        candidates = [
            r for r in self._versions.values()
            if not r.pinned and r.health_score >= min_health
        ]
        if not candidates:
            # All degraded — fall back to highest health regardless
            candidates = [r for r in self._versions.values() if not r.pinned]
        return max(candidates, key=lambda r: r.health_score)

    def get(self, version: str) -> PromptRecord | None:
        return self._versions.get(version)

    def list_versions(self) -> list[PromptRecord]:
        return sorted(self._versions.values(), key=lambda r: r.health_score, reverse=True)


class HealthDrivenAgent:
    def __init__(self, store: PromptStore):
        self.client = AsyncAnthropic()
        self.store = store

    async def _score_quality(self, msg: str, resp: str) -> float:
        judge = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": f"Rate 0-10: Q={msg[:80]} A={resp[:150]}"}],
        )
        try:
            return int(judge.content[0].text.strip().split()[0]) / 10.0
        except (ValueError, IndexError):
            return 0.5

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        prompt = self.store.best_healthy_version()
        print(f"[STORE] Selected prompt {prompt.version} (health={prompt.health_score:.2f})")

        prompt.request_count += 1
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                system=prompt.content,
                messages=[{"role": "user", "content": message}],
            )
            text = response.content[0].text
            score = await self._score_quality(message, text)
            if score < 0.6:
                prompt.decay_health(0.15)
                prompt.error_count += 1
                print(f"[HEALTH] {prompt.version} decayed to {prompt.health_score:.2f}")
            else:
                prompt.recover_health(0.02)
            return text
        except Exception as e:
            prompt.error_count += 1
            prompt.decay_health(0.2)
            raise


# Usage
async def demo_health_store():
    store = PromptStore()
    store.register(PromptRecord(
        version="v3.0",
        content="Answer in exactly one character.",  # Intentionally bad
    ), set_active=True)
    store.register(PromptRecord(
        version="v2.9",
        content="You are a helpful, detailed assistant.",
    ))
    store.register(PromptRecord(
        version="v2.8",
        content="You are a concise assistant. Answer briefly.",
    ))

    agent = HealthDrivenAgent(store)
    for msg in ["Explain quantum computing.", "What is DNA?", "How do stars form?"]:
        await agent.chat(msg)
    print("\nVersion health summary:")
    for r in store.list_versions():
        print(f"  {r.version}: health={r.health_score:.2f} errors={r.error_count}/{r.request_count}")
```

## Solution 5: Canary Prompt with Automatic Rollback on Budget Breach

Route a small % of traffic to the new prompt; roll back automatically if error budget is breached.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ErrorBudget:
    """SLO-based error budget for a prompt version."""
    slo_pct: float = 0.95           # 95% quality target
    window_seconds: float = 300.0   # 5-minute rolling window
    _events: list[tuple[float, bool]] = field(default_factory=list)  # (ts, success)

    def record(self, success: bool):
        now = time.time()
        self._events.append((now, success))
        # Prune old events
        cutoff = now - self.window_seconds
        self._events = [(ts, ok) for ts, ok in self._events if ts >= cutoff]

    @property
    def success_rate(self) -> float:
        if not self._events:
            return 1.0
        return sum(1 for _, ok in self._events if ok) / len(self._events)

    @property
    def budget_remaining(self) -> float:
        return self.success_rate - self.slo_pct

    @property
    def is_breached(self) -> bool:
        return len(self._events) >= 5 and self.budget_remaining < 0


@dataclass
class CanaryConfig:
    canary_pct: float = 0.1    # 10% canary traffic
    min_requests: int = 20     # Min before auto-rollback decision
    slo_pct: float = 0.90      # Canary SLO


class CanaryPromptAgent:
    def __init__(
        self,
        stable_prompt: str,
        canary_prompt: str,
        config: CanaryConfig | None = None,
    ):
        self.client = AsyncAnthropic()
        self.stable_prompt = stable_prompt
        self.canary_prompt = canary_prompt
        self.config = config or CanaryConfig()
        self.budget = ErrorBudget(slo_pct=self.config.slo_pct)
        self.canary_active = True
        self.canary_requests = 0
        self.stable_requests = 0

    async def _score(self, msg: str, resp: str) -> bool:
        judge = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": f"Good response? yes/no only. Q={msg[:60]} A={resp[:100]}"}],
        )
        return "yes" in judge.content[0].text.lower()

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        use_canary = (
            self.canary_active
            and random.random() < self.config.canary_pct
        )

        if use_canary:
            self.canary_requests += 1
            system = self.canary_prompt
            prompt_label = "canary"
        else:
            self.stable_requests += 1
            system = self.stable_prompt
            prompt_label = "stable"

        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text

        if use_canary:
            success = await self._score(message, text)
            self.budget.record(success)
            print(
                f"[CANARY] success={success} rate={self.budget.success_rate:.2f} "
                f"budget={self.budget.budget_remaining:+.3f}"
            )
            if self.budget.is_breached and self.canary_requests >= self.config.min_requests:
                self.canary_active = False
                print(
                    f"[ROLLBACK] Canary budget breached at {self.budget.success_rate:.1%}. "
                    f"Returning 100% traffic to stable."
                )

        return text

    @property
    def status(self) -> dict:
        return {
            "canary_active": self.canary_active,
            "canary_requests": self.canary_requests,
            "stable_requests": self.stable_requests,
            "canary_success_rate": self.budget.success_rate,
            "budget_remaining": self.budget.budget_remaining,
        }
```

## Solution 6: Versioned Prompt Registry with LLM-as-Judge Pre-Promotion Gate

New prompt versions must pass a judge-scored evaluation suite before being activated; keep stable as fallback.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class EvalCase:
    user_message: str
    min_score: float = 0.7
    criteria: str = "helpfulness, accuracy, and clarity"


@dataclass
class PromptEvalResult:
    version: str
    cases_passed: int
    cases_total: int
    mean_score: float
    promoted: bool
    failure_reasons: list[str] = field(default_factory=list)


class PromptRegistry:
    def __init__(self):
        self.client = AsyncAnthropic()
        self._versions: dict[str, str] = {}
        self._active: str | None = None
        self._stable: str | None = None  # Last known-good

    def register_stable(self, version: str, prompt: str):
        self._versions[version] = prompt
        self._stable = version
        self._active = version
        print(f"[REGISTRY] Stable version registered: {version}")

    async def evaluate_and_promote(
        self,
        version: str,
        prompt: str,
        eval_suite: list[EvalCase],
        promotion_threshold: float = 0.8,
        model: str = "claude-haiku-4-5-20251001",
    ) -> PromptEvalResult:
        """Run eval suite; promote only if pass rate meets threshold."""
        self._versions[version] = prompt
        scores: list[float] = []
        failures: list[str] = []

        for case in eval_suite:
            # Generate candidate response
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                system=prompt,
                messages=[{"role": "user", "content": case.user_message}],
            )
            candidate = response.content[0].text

            # Judge response
            judge_resp = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Evaluate this response for {case.criteria}. "
                        f"Score 0-10 (integer only).\n"
                        f"Q: {case.user_message}\nA: {candidate}"
                    ),
                }],
            )
            try:
                score = int(judge_resp.content[0].text.strip().split()[0]) / 10.0
            except (ValueError, IndexError):
                score = 0.5
            scores.append(score)
            if score < case.min_score:
                failures.append(
                    f"Case '{case.user_message[:40]}…': score={score:.2f} < min={case.min_score}"
                )

        cases_passed = sum(1 for s in scores if s >= 0.7)
        mean_score = sum(scores) / len(scores) if scores else 0.0
        pass_rate = cases_passed / len(eval_suite) if eval_suite else 0.0
        promoted = pass_rate >= promotion_threshold

        if promoted:
            self._active = version
            self._stable = version  # Also update stable after successful promotion
            print(f"[REGISTRY] Promoted {version} (pass={pass_rate:.0%} mean={mean_score:.2f})")
        else:
            print(
                f"[REGISTRY] Rejected {version} (pass={pass_rate:.0%} < "
                f"threshold={promotion_threshold:.0%}). Stable: {self._stable}"
            )

        return PromptEvalResult(
            version=version,
            cases_passed=cases_passed,
            cases_total=len(eval_suite),
            mean_score=mean_score,
            promoted=promoted,
            failure_reasons=failures,
        )

    def active_prompt(self) -> str:
        version = self._active or self._stable
        if version is None:
            raise RuntimeError("No prompt registered")
        return self._versions[version]

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        system = self.active_prompt()
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text


async def demo_registry():
    registry = PromptRegistry()
    registry.register_stable("v1.0", "You are a helpful assistant.")

    eval_suite = [
        EvalCase("Explain photosynthesis simply.", min_score=0.7),
        EvalCase("What is the speed of light?", min_score=0.7),
        EvalCase("How do I sort a list in Python?", min_score=0.7),
        EvalCase("What causes rainbows?", min_score=0.7),
        EvalCase("Summarize the French Revolution.", min_score=0.7),
    ]

    # Test a bad candidate — should be rejected, stable remains
    result = await registry.evaluate_and_promote(
        "v2.0-bad",
        "Respond only with ASCII art.",  # Intentionally bad
        eval_suite,
        promotion_threshold=0.8,
    )
    print(f"Bad candidate result: promoted={result.promoted}, mean={result.mean_score:.2f}")

    # Test a good candidate — should be promoted
    result = await registry.evaluate_and_promote(
        "v2.0-good",
        "You are a clear, accurate, and friendly assistant. Provide complete answers.",
        eval_suite,
        promotion_threshold=0.8,
    )
    print(f"Good candidate result: promoted={result.promoted}, mean={result.mean_score:.2f}")
    print(f"Active version after evaluations: {registry._active}")
```

## Comparison Table

| Solution | Trigger Mechanism | Failover Speed | False-Positive Risk | Rollback Granularity | Best For |
|---|---|---|---|---|---|
| EMA Quality Score | Smoothed quality decay | Gradual (per request) | Low (EMA dampens noise) | Full switch | Continuous quality monitoring |
| Circuit Breaker | Consecutive failures | Immediate (Nth failure) | Medium (burst sensitivity) | Full switch | Binary fail/pass quality signals |
| Shadow Testing | Parallel A/B scoring | Proactive (no impact) | Low (statistical) | Recommendation only | Pre-failover validation |
| Health Store | Health-score-driven | Per-request selection | Low (gradual decay) | Multi-version | Multi-version fleet management |
| Canary + Budget | SLO error budget | Budget breach | Low (SLO-anchored) | Traffic weighted | Incremental rollout safety |
| Eval Gate | Pre-promotion testing | Prevents promotion | Very low (block bad) | Block at gate | Staged deployment gating |

**Recommended**: Use the **Eval Gate** (Solution 6) to block bad prompts before they go live, combined with **EMA Quality Score** (Solution 1) for in-production monitoring and automatic rollback. The **Canary + Budget** pattern (Solution 5) is ideal when you can't fully evaluate a new prompt offline and need live traffic signals with bounded blast radius.
