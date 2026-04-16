---
layout: solution
title: "Agent Doesn't Implement Progressive Rollout with Automatic Rollback"
description: "How to ship prompt changes, model upgrades, and agent behavior changes to a small percentage of traffic first, then roll back automatically if quality metrics degrade."
tags: [reliability, deployment, rollout, feature-flags, canary, rollback, quality]
difficulty: advanced
solution_count: 6
---

## Problem

Agent teams deploy prompt changes, model upgrades, and new tool schemas all-at-once to 100% of traffic. A regression discovered post-deployment affects every user immediately and requires a manual hotfix that takes minutes or hours. There is no controlled exposure, no automatic quality gate, and no rollback signal tied to real production metrics.

```python
# Bad: immediate 100% rollout with no quality gate
SYSTEM_PROMPT = load_prompt("v2_prompt.txt")  # live for everyone instantly
# If v2 is worse, every user suffers until someone notices and reverts manually
```

---

## Solution 1 — Percentage-Based Traffic Split with Manual Control

Split traffic by hashing the session/user ID into buckets, enabling instant percentage changes and immediate 0% rollback.

```python
import hashlib
from dataclasses import dataclass
from typing import Any

@dataclass
class PromptVariant:
    name: str
    system_prompt: str
    rollout_pct: float  # 0.0 - 100.0

VARIANTS: list[PromptVariant] = [
    PromptVariant("v1_stable", "You are a helpful assistant. Be concise.", 90.0),
    PromptVariant("v2_candidate", "You are a helpful assistant. Think step by step before answering.", 10.0),
]

def select_variant(session_id: str) -> PromptVariant:
    """Stable assignment: same session always gets the same variant."""
    h = int(hashlib.md5(session_id.encode()).hexdigest(), 16)
    bucket = h % 100  # 0..99

    cumulative = 0.0
    for variant in VARIANTS:
        cumulative += variant.rollout_pct
        if bucket < cumulative:
            return variant

    return VARIANTS[0]  # fallback to stable

def rollback(variant_name: str) -> None:
    """Set variant rollout to 0% — instant rollback without code deploy."""
    for v in VARIANTS:
        if v.name == variant_name:
            v.rollout_pct = 0.0
        elif v.name == "v1_stable":
            v.rollout_pct = 100.0
    print(f"Rolled back {variant_name} — all traffic now on v1_stable")

def increase_rollout(variant_name: str, new_pct: float) -> None:
    """Gradually ramp a variant: 1% -> 5% -> 10% -> 50% -> 100%."""
    remaining = 100.0 - new_pct
    for v in VARIANTS:
        if v.name == variant_name:
            v.rollout_pct = new_pct
        elif v.name == "v1_stable":
            v.rollout_pct = remaining

# Usage
variant = select_variant("user-session-abc123")
print(f"Using: {variant.name}")

# Gradual ramp schedule:
# increase_rollout("v2_candidate", 1.0)   # 1%
# increase_rollout("v2_candidate", 5.0)   # 5%
# increase_rollout("v2_candidate", 25.0)  # 25%
# increase_rollout("v2_candidate", 100.0) # full
```

---

## Solution 2 — Metric-Gated Auto-Rollback with EMA Quality Score

Track output quality metrics (error rate, user thumbs-down, format violations) per variant using EMA. Automatically roll back the candidate if it scores below the stable variant by a threshold.

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class VariantMetrics:
    name: str
    alpha: float = 0.1  # EMA smoothing
    quality_score: float = 1.0  # starts at perfect
    error_rate: float = 0.0
    calls: int = 0

    def record(self, success: bool, quality: float) -> None:
        self.calls += 1
        outcome = quality if success else 0.0
        self.quality_score = self.alpha * outcome + (1 - self.alpha) * self.quality_score
        self.error_rate = self.alpha * (0 if success else 1) + (1 - self.alpha) * self.error_rate

class AutoRolloutController:
    ROLLBACK_THRESHOLD = 0.15  # candidate must not be >15% worse than stable

    def __init__(self):
        self._variants: dict[str, PromptVariant] = {
            "v1_stable": PromptVariant("v1_stable", "Be concise.", 90.0),
            "v2_candidate": PromptVariant("v2_candidate", "Think step by step.", 10.0),
        }
        self._metrics: dict[str, VariantMetrics] = {
            name: VariantMetrics(name) for name in self._variants
        }
        self._rolled_back = False

    def select(self, session_id: str) -> PromptVariant:
        if self._rolled_back:
            return self._variants["v1_stable"]
        return select_variant(session_id)  # from Solution 1

    def record_outcome(self, variant_name: str, success: bool, quality: float) -> None:
        self._metrics[variant_name].record(success, quality)
        self._check_rollback()

    def _check_rollback(self) -> None:
        if self._rolled_back:
            return
        stable = self._metrics.get("v1_stable")
        candidate = self._metrics.get("v2_candidate")
        if not stable or not candidate or candidate.calls < 20:
            return  # wait for statistical significance

        gap = stable.quality_score - candidate.quality_score
        if gap > self.ROLLBACK_THRESHOLD:
            print(
                f"AUTO-ROLLBACK: v2_candidate quality={candidate.quality_score:.3f} "
                f"vs v1_stable={stable.quality_score:.3f} (gap={gap:.3f} > {self.ROLLBACK_THRESHOLD})"
            )
            self._rolled_back = True
            self._variants["v1_stable"].rollout_pct = 100.0
            self._variants["v2_candidate"].rollout_pct = 0.0

    def status(self) -> None:
        for m in self._metrics.values():
            print(f"{m.name}: quality={m.quality_score:.3f} errors={m.error_rate:.3f} calls={m.calls}")

controller = AutoRolloutController()

async def handle(session_id: str, message: str) -> str:
    variant = controller.select(session_id)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=variant.system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        output = response.content[0].text
        # Quality signal: penalize very short or very long outputs (heuristic)
        quality = 1.0 if 10 < len(output) < 2000 else 0.5
        controller.record_outcome(variant.name, success=True, quality=quality)
        return output
    except Exception as e:
        controller.record_outcome(variant.name, success=False, quality=0.0)
        raise
```

---

## Solution 3 — LLM-as-Judge Quality Gate Between Variants

After each response, use a fast model (haiku) to score the output quality and use those scores as the rollback signal.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

JUDGE_PROMPT = """\
Rate this AI assistant response on a scale of 1-10.
User question: {question}
Response: {response}
Criteria: helpfulness, accuracy, conciseness, format.
Reply with only a JSON object: {{"score": <1-10>, "reason": "<one sentence>"}}"""

async def judge_response(question: str, response: str) -> float:
    """Returns quality score 0.0-1.0."""
    try:
        result = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": JUDGE_PROMPT.format(question=question, response=response)
            }],
        )
        import json
        text = result.content[0].text.strip()
        data = json.loads(text[text.find("{"):text.rfind("}")+1])
        return float(data["score"]) / 10.0
    except Exception:
        return 0.5  # neutral on judge failure

@dataclass
class RolloutVariant:
    name: str
    system_prompt: str
    rollout_pct: float
    score_history: list[float] = None

    def __post_init__(self):
        self.score_history = []

    def avg_score(self) -> float:
        if not self.score_history:
            return 1.0
        return sum(self.score_history[-50:]) / len(self.score_history[-50:])

class JudgedRolloutController:
    ROLLBACK_DELTA = 0.10  # auto-rollback if candidate is 10% worse

    def __init__(self):
        self.stable = RolloutVariant("v1_stable", "Be concise.", 90.0)
        self.candidate = RolloutVariant("v2_candidate", "Think step by step.", 10.0)

    async def run(self, session_id: str, question: str) -> str:
        variant = self.candidate if (int(session_id[-1], 16) % 10 < 1) else self.stable
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=variant.system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        output = response.content[0].text

        # Judge quality asynchronously (don't block the user)
        asyncio.create_task(self._judge_and_update(variant, question, output))
        return output

    async def _judge_and_update(self, variant: RolloutVariant, q: str, r: str) -> None:
        score = await judge_response(q, r)
        variant.score_history.append(score)

        if len(self.candidate.score_history) >= 10:
            delta = self.stable.avg_score() - self.candidate.avg_score()
            if delta > self.ROLLBACK_DELTA:
                print(f"Quality gate failed: candidate avg={self.candidate.avg_score():.2f} "
                      f"stable avg={self.stable.avg_score():.2f} — rolling back")
                self.stable.rollout_pct = 100.0
                self.candidate.rollout_pct = 0.0
```

---

## Solution 4 — Shadow Mode: Run Both Variants, Ship Only Stable

In shadow mode, the candidate variant runs on every request but its output is discarded. This collects real-traffic quality data with zero user risk before any exposure.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class ShadowVariantResult:
    variant: str
    output: str
    score: float = 0.0
    latency_ms: float = 0.0

class ShadowRolloutRunner:
    """Run candidate in shadow; only serve stable to users."""

    def __init__(self, stable_prompt: str, candidate_prompt: str):
        self._stable_prompt = stable_prompt
        self._candidate_prompt = candidate_prompt
        self._shadow_log: list[dict] = []

    async def _call_variant(self, system: str, message: str) -> tuple[str, float]:
        import time
        t0 = time.monotonic()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        latency = (time.monotonic() - t0) * 1000
        return response.content[0].text, latency

    async def handle(self, message: str) -> str:
        # Run both variants in parallel
        stable_task = asyncio.create_task(
            self._call_variant(self._stable_prompt, message)
        )
        shadow_task = asyncio.create_task(
            self._call_variant(self._candidate_prompt, message)
        )

        # Only await stable for the user response
        stable_output, stable_latency = await stable_task

        # Log shadow result without blocking user
        async def log_shadow():
            try:
                shadow_output, shadow_latency = await shadow_task
                self._shadow_log.append({
                    "input": message[:100],
                    "stable_output": stable_output[:200],
                    "candidate_output": shadow_output[:200],
                    "stable_latency_ms": stable_latency,
                    "candidate_latency_ms": shadow_latency,
                })
            except Exception as e:
                pass  # shadow failures never affect users

        asyncio.create_task(log_shadow())
        return stable_output  # user only sees stable

    def shadow_report(self) -> None:
        n = len(self._shadow_log)
        if not n:
            print("No shadow data yet")
            return
        avg_stable_lat = sum(e["stable_latency_ms"] for e in self._shadow_log) / n
        avg_cand_lat = sum(e["candidate_latency_ms"] for e in self._shadow_log) / n
        print(f"Shadow report: {n} requests")
        print(f"  Stable avg latency:    {avg_stable_lat:.0f}ms")
        print(f"  Candidate avg latency: {avg_cand_lat:.0f}ms")
        print("  Sample comparison:")
        for entry in self._shadow_log[:3]:
            print(f"    stable: {entry['stable_output'][:60]!r}")
            print(f"    cand:   {entry['candidate_output'][:60]!r}")
```

---

## Solution 5 — Staged Rollout with Automated Health Checks at Each Stage

Move through defined stages (1% → 5% → 20% → 50% → 100%) automatically, but gate each promotion on passing health checks.

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class RolloutStage:
    pct: float
    min_requests: int   # minimum sample before promotion
    max_error_rate: float
    min_quality_score: float

STAGES = [
    RolloutStage(pct=1.0,   min_requests=10,  max_error_rate=0.05, min_quality_score=0.85),
    RolloutStage(pct=5.0,   min_requests=50,  max_error_rate=0.03, min_quality_score=0.87),
    RolloutStage(pct=20.0,  min_requests=100, max_error_rate=0.02, min_quality_score=0.88),
    RolloutStage(pct=50.0,  min_requests=200, max_error_rate=0.02, min_quality_score=0.89),
    RolloutStage(pct=100.0, min_requests=0,   max_error_rate=1.0,  min_quality_score=0.0),
]

class StagedRolloutManager:
    def __init__(self, candidate_name: str):
        self._candidate = candidate_name
        self._stage_idx = 0
        self._candidate_calls = 0
        self._candidate_errors = 0
        self._quality_scores: list[float] = []
        self._started_at = time.time()
        self._rolled_back = False

    @property
    def current_pct(self) -> float:
        return 0.0 if self._rolled_back else STAGES[self._stage_idx].pct

    def record(self, success: bool, quality: float) -> None:
        self._candidate_calls += 1
        if not success:
            self._candidate_errors += 1
        self._quality_scores.append(quality)
        self._try_promote()

    def _health_ok(self, stage: RolloutStage) -> bool:
        if self._candidate_calls < stage.min_requests:
            return False
        error_rate = self._candidate_errors / max(self._candidate_calls, 1)
        avg_quality = sum(self._quality_scores[-100:]) / max(len(self._quality_scores[-100:]), 1)
        return error_rate <= stage.max_error_rate and avg_quality >= stage.min_quality_score

    def _try_promote(self) -> None:
        if self._rolled_back or self._stage_idx >= len(STAGES) - 1:
            return

        current = STAGES[self._stage_idx]

        # Check if health is too bad for current stage
        error_rate = self._candidate_errors / max(self._candidate_calls, 1)
        if (self._candidate_calls >= current.min_requests and
                error_rate > current.max_error_rate * 2):  # 2x threshold = rollback
            print(f"ROLLBACK: error_rate={error_rate:.3f} at stage {current.pct}%")
            self._rolled_back = True
            return

        if self._health_ok(current):
            self._stage_idx += 1
            next_stage = STAGES[self._stage_idx]
            print(f"PROMOTED: {self._candidate} -> {next_stage.pct}%")
            # Reset counters for next stage evaluation
            self._candidate_calls = 0
            self._candidate_errors = 0
            self._quality_scores = []

    def status(self) -> str:
        if self._rolled_back:
            return f"{self._candidate}: ROLLED BACK"
        stage = STAGES[self._stage_idx]
        return (f"{self._candidate}: {stage.pct}% "
                f"(calls={self._candidate_calls}, "
                f"errors={self._candidate_errors})")

manager = StagedRolloutManager("v2_prompt")
print(f"Starting at {manager.current_pct}%")
```

---

## Solution 6 — Feature Flag Integration with LaunchDarkly-Style SDK

Decouple rollout control from code: read rollout configuration from a remote flag store that ops/ML teams can adjust without deploys.

```python
import asyncio
import json
import time
import hashlib
import httpx
from dataclasses import dataclass
from typing import Any

@dataclass
class FeatureFlag:
    key: str
    default_value: Any
    rules: list[dict]  # [{condition: ..., value: ...}]
    last_fetched: float = 0.0

class FeatureFlagClient:
    """Polls a remote flag endpoint and evaluates flags locally."""

    def __init__(self, sdk_key: str, endpoint: str, poll_interval: float = 30.0):
        self._sdk_key = sdk_key
        self._endpoint = endpoint
        self._poll_interval = poll_interval
        self._flags: dict[str, FeatureFlag] = {}
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {sdk_key}"}
        )

    async def start(self) -> None:
        await self._fetch_flags()
        asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._fetch_flags()
            except Exception as e:
                print(f"Flag fetch failed: {e} — using cached flags")

    async def _fetch_flags(self) -> None:
        try:
            response = await self._http.get(f"{self._endpoint}/flags", timeout=5.0)
            data = response.json()
            for key, spec in data.items():
                self._flags[key] = FeatureFlag(
                    key=key,
                    default_value=spec["default"],
                    rules=spec.get("rules", []),
                    last_fetched=time.time(),
                )
        except Exception:
            pass  # keep existing flags on fetch failure

    def evaluate(self, flag_key: str, context: dict, default: Any = None) -> Any:
        flag = self._flags.get(flag_key)
        if not flag:
            return default

        for rule in flag.rules:
            if self._matches_rule(rule, context):
                return rule["value"]

        return flag.default_value

    def _matches_rule(self, rule: dict, context: dict) -> bool:
        condition = rule.get("condition", {})

        # Percentage rollout
        if "pct" in condition:
            key = context.get("session_id", "")
            bucket = int(hashlib.md5(key.encode()).hexdigest(), 16) % 100
            return bucket < condition["pct"]

        # User segment
        if "user_segment" in condition:
            return context.get("segment") in condition["user_segment"]

        # Explicit user list
        if "user_ids" in condition:
            return context.get("user_id") in condition["user_ids"]

        return False

    async def stop(self) -> None:
        await self._http.aclose()

# Flag configuration (stored remotely, editable without deploy):
# {
#   "agent_v2_prompt": {
#     "default": "v1_stable",
#     "rules": [
#       {"condition": {"user_segment": ["beta_testers"]}, "value": "v2_candidate"},
#       {"condition": {"pct": 10}, "value": "v2_candidate"}
#     ]
#   }
# }

PROMPT_REGISTRY = {
    "v1_stable": "You are a helpful assistant. Be concise.",
    "v2_candidate": "You are a helpful assistant. Think step by step.",
}

class FlaggedAgentRunner:
    def __init__(self, flag_client: FeatureFlagClient):
        self._flags = flag_client

    def get_system_prompt(self, session_id: str, user_id: str, segment: str) -> str:
        variant = self._flags.evaluate(
            "agent_v2_prompt",
            context={"session_id": session_id, "user_id": user_id, "segment": segment},
            default="v1_stable",
        )
        return PROMPT_REGISTRY.get(variant, PROMPT_REGISTRY["v1_stable"])

# Rollback = set flag default to "v1_stable" in the flag dashboard — instant, no deploy
```

---

## Comparison

| Approach | Rollback Speed | Quality Signal | User Risk | Ops Complexity | Best For |
|---|---|---|---|---|---|
| Percentage split | Instant (in-process) | Manual | Low | Low | Simple prompt A/B |
| EMA auto-rollback | Automatic (~20 calls) | Heuristic | Low | Medium | Metric-gated rollout |
| LLM-as-judge | Automatic (~10 calls) | **LLM quality score** | Low | Medium | Quality-sensitive changes |
| Shadow mode | N/A (no exposure) | **Full pre-production data** | **Zero** | Medium | High-risk model upgrades |
| Staged rollout | Per-stage gate | Error rate + quality | Low | Medium | Systematic multi-step ramp |
| Feature flags | **Instant (remote)** | External dashboards | Low | **High** | Ops-team-controlled rollout |
