---
title: "Agent Doesn't Implement Replay-Based Regression Testing"
description: "How to capture, store, and replay production agent interactions to automatically detect regressions when prompts, models, or tool implementations change — turning real traffic into a continuous regression test suite."
date: 2025-01-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-replay-based-regression-testing
tags:
  - observability
  - regression-testing
  - replay
  - golden-tests
  - prompt-testing
  - production-traffic
  - quality-assurance
symptoms:
  - "Prompt changes silently break behavior for specific user patterns not covered by unit tests"
  - "Model version upgrades degrade quality on edge cases only seen in production"
  - "No automated way to verify agent behavior after a deploy without manual QA"
  - "Tool implementation changes cause subtle behavior shifts that go unnoticed"
  - "Production traffic contains scenarios that synthetic tests never cover"
  - "Cannot confidently compare old vs. new model outputs side-by-side at scale"
---

## Why This Happens

AI agents have non-deterministic outputs, making traditional assertion-based regression tests brittle. When you change a prompt, upgrade a model, or refactor a tool, there is no automated safety net that catches subtle degradation — only integration test suites written by engineers who cannot anticipate every real user pattern.

Replay-based regression testing captures real production interactions (inputs + expected outputs) and replays them against new agent versions. Outputs are compared using semantic similarity, LLM-as-judge scoring, or structural checks rather than exact string matching. This converts your production traffic into an ever-growing regression suite that exercises precisely the patterns real users generate.

---

## Solution 1: Interaction Recorder

Capture every agent interaction to a replay store during production operation, with sampling controls to manage volume.

```python
import asyncio
import hashlib
import json
import time
import uuid
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

@dataclass
class RecordedInteraction:
    interaction_id: str
    session_id: str
    timestamp: float
    model: str
    system_prompt: str
    messages: list[dict]          # Full conversation history
    tool_definitions: list[dict]  # Tool schemas used
    tool_results: dict[str, Any]  # Actual tool outputs during capture
    final_response: str
    metadata: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    @property
    def interaction_hash(self) -> str:
        """Fingerprint for dedup and lookup."""
        payload = json.dumps({
            "system": self.system_prompt,
            "messages": self.messages,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)


class InteractionRecorder:
    """
    Records agent interactions with configurable sampling.
    Stores interactions for later replay in regression tests.
    """

    def __init__(
        self,
        store: "ReplayStore",
        sample_rate: float = 0.1,     # Record 10% of traffic by default
        max_stored: int = 50_000,
    ):
        self.store = store
        self.sample_rate = sample_rate
        self.max_stored = max_stored
        self._recorded = 0

    def should_record(self) -> bool:
        return random.random() < self.sample_rate

    async def record(
        self,
        session_id: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tool_definitions: list[dict],
        tool_results: dict[str, Any],
        final_response: str,
        tags: list[str] | None = None,
        force: bool = False,
    ) -> Optional[str]:
        """
        Record an interaction. Returns interaction_id if recorded, None if sampled out.
        force=True bypasses sampling (for interesting/flagged interactions).
        """
        if not force and not self.should_record():
            return None
        if self._recorded >= self.max_stored:
            return None

        interaction = RecordedInteraction(
            interaction_id=str(uuid.uuid4()),
            session_id=session_id,
            timestamp=time.time(),
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tool_definitions=tool_definitions,
            tool_results=tool_results,
            final_response=final_response,
            tags=tags or [],
        )
        await self.store.save(interaction)
        self._recorded += 1
        return interaction.interaction_id

    async def record_flagged(self, interaction_id: str, tag: str) -> None:
        """Tag an interaction as interesting (e.g., user gave negative feedback)."""
        await self.store.add_tag(interaction_id, tag)


class ReplayStore:
    """In-memory replay store (replace with database in production)."""

    def __init__(self):
        self._interactions: dict[str, RecordedInteraction] = {}

    async def save(self, interaction: RecordedInteraction) -> None:
        self._interactions[interaction.interaction_id] = interaction

    async def get(self, interaction_id: str) -> Optional[RecordedInteraction]:
        return self._interactions.get(interaction_id)

    async def get_by_tag(self, tag: str) -> list[RecordedInteraction]:
        return [i for i in self._interactions.values() if tag in i.tags]

    async def get_sample(self, n: int, tags: list[str] | None = None) -> list[RecordedInteraction]:
        candidates = list(self._interactions.values())
        if tags:
            candidates = [i for i in candidates if any(t in i.tags for t in tags)]
        random.shuffle(candidates)
        return candidates[:n]

    async def add_tag(self, interaction_id: str, tag: str) -> None:
        if interaction_id in self._interactions:
            self._interactions[interaction_id].tags.append(tag)

    def __len__(self) -> int:
        return len(self._interactions)
```

---

## Solution 2: Replay Engine

Replay recorded interactions against a new agent configuration and collect outputs for comparison.

```python
import asyncio
from dataclasses import dataclass

@dataclass
class ReplayResult:
    interaction_id: str
    original_response: str
    replayed_response: str
    original_model: str
    replayed_model: str
    replay_timestamp: float
    tool_calls_matched: bool
    metadata: dict = field(default_factory=dict)


class ReplayEngine:
    """
    Replays captured interactions against a new agent/model configuration.
    Injects recorded tool results so replays are deterministic and do not
    make real external API calls.
    """

    def __init__(self, agent_factory, use_recorded_tool_results: bool = True):
        self.agent_factory = agent_factory
        self.use_recorded_tool_results = use_recorded_tool_results

    async def replay_one(
        self,
        interaction: RecordedInteraction,
        new_model: str,
        new_system_prompt: Optional[str] = None,
    ) -> ReplayResult:
        """Replay a single interaction with a new model or system prompt."""
        agent = self.agent_factory(
            model=new_model,
            system_prompt=new_system_prompt or interaction.system_prompt,
            tool_results_override=(
                interaction.tool_results if self.use_recorded_tool_results else None
            ),
        )

        replayed_response = await agent.run(interaction.messages)

        # Detect whether the same tools were called (structural check)
        tool_calls_matched = True  # Simplified — check actual tool call patterns

        return ReplayResult(
            interaction_id=interaction.interaction_id,
            original_response=interaction.final_response,
            replayed_response=replayed_response,
            original_model=interaction.model,
            replayed_model=new_model,
            replay_timestamp=time.time(),
            tool_calls_matched=tool_calls_matched,
        )

    async def replay_batch(
        self,
        interactions: list[RecordedInteraction],
        new_model: str,
        new_system_prompt: Optional[str] = None,
        concurrency: int = 5,
    ) -> list[ReplayResult]:
        """Replay multiple interactions concurrently."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _replay_with_limit(interaction):
            async with semaphore:
                try:
                    return await self.replay_one(interaction, new_model, new_system_prompt)
                except Exception as exc:
                    return ReplayResult(
                        interaction_id=interaction.interaction_id,
                        original_response=interaction.final_response,
                        replayed_response=f"ERROR: {exc}",
                        original_model=interaction.model,
                        replayed_model=new_model,
                        replay_timestamp=time.time(),
                        tool_calls_matched=False,
                        metadata={"error": str(exc)},
                    )

        tasks = [_replay_with_limit(i) for i in interactions]
        return await asyncio.gather(*tasks)
```

---

## Solution 3: Semantic Similarity Comparator

Compare original vs. replayed responses using embedding cosine similarity — tolerates paraphrasing while catching semantic regressions.

```python
import numpy as np
from typing import Callable, Awaitable

@dataclass
class ComparisonResult:
    interaction_id: str
    similarity_score: float       # 0-1 cosine similarity
    regression_detected: bool
    regression_severity: str      # "none", "minor", "major", "critical"
    details: dict = field(default_factory=dict)


class SemanticSimilarityComparator:
    """
    Compares original and replayed responses using embedding similarity.
    Configurable thresholds for minor/major/critical regression detection.
    """

    SEVERITY_THRESHOLDS = {
        "critical": 0.70,   # < 0.70 similarity
        "major":    0.85,
        "minor":    0.95,
        "none":     1.01,
    }

    def __init__(self, embed_fn: Callable[[str], Awaitable[list[float]]]):
        self.embed = embed_fn

    async def compare(self, result: ReplayResult) -> ComparisonResult:
        orig_emb, replay_emb = await asyncio.gather(
            self.embed(result.original_response),
            self.embed(result.replayed_response),
        )

        score = self._cosine_similarity(orig_emb, replay_emb)
        severity = self._classify(score)

        return ComparisonResult(
            interaction_id=result.interaction_id,
            similarity_score=round(score, 4),
            regression_detected=severity != "none",
            regression_severity=severity,
            details={
                "original_length": len(result.original_response),
                "replayed_length": len(result.replayed_response),
                "length_ratio": len(result.replayed_response) / max(1, len(result.original_response)),
                "tool_calls_matched": result.tool_calls_matched,
            },
        )

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        va, vb = np.array(a), np.array(b)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        if denom == 0:
            return 0.0
        return float(np.dot(va, vb) / denom)

    def _classify(self, score: float) -> str:
        for severity, threshold in self.SEVERITY_THRESHOLDS.items():
            if score < threshold:
                return severity
        return "none"

    async def compare_batch(self, results: list[ReplayResult]) -> list[ComparisonResult]:
        return await asyncio.gather(*[self.compare(r) for r in results])
```

---

## Solution 4: LLM-as-Judge Evaluator

For subjective quality assessment, use an LLM to judge whether the replayed response is better, worse, or equivalent to the original.

```python
import anthropic

class LLMJudgeEvaluator:
    """
    Uses an LLM to evaluate whether the replayed response represents a regression.
    More nuanced than embedding similarity — can detect factual errors, tone changes,
    missing information, or hallucinations.
    """

    JUDGE_PROMPT = """You are evaluating whether an AI assistant's response has regressed.

Original response (from previous version):
<original>
{original}
</original>

New response (from updated version):
<new>
{new}
</new>

User's original message:
<user_message>
{user_message}
</user_message>

Compare the two responses and rate the new response:
- BETTER: The new response is clearly better (more accurate, complete, or helpful)
- EQUIVALENT: The responses are functionally equivalent or differ only in style
- MINOR_REGRESSION: The new response is slightly worse but still acceptable
- MAJOR_REGRESSION: The new response is significantly worse (missing key info, wrong facts)
- CRITICAL_REGRESSION: The new response is dangerous, harmful, or completely wrong

Reply with JSON: {{"verdict": "...", "reasoning": "...", "confidence": 0-1}}"""

    def __init__(self, client: anthropic.Anthropic, judge_model: str = "claude-3-haiku-20240307"):
        self.client = client
        self.judge_model = judge_model

    async def judge(self, result: ReplayResult, original_user_message: str) -> dict:
        prompt = self.JUDGE_PROMPT.format(
            original=result.original_response,
            new=result.replayed_response,
            user_message=original_user_message,
        )

        response = await asyncio.to_thread(
            self.client.messages.create,
            model=self.judge_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            import re
            # Extract JSON from response if wrapped in markdown
            m = re.search(r'\{[^}]+\}', text, re.DOTALL)
            if m:
                return json.loads(m.group())
            return {"verdict": "UNKNOWN", "reasoning": text, "confidence": 0.0}

    async def judge_batch(
        self,
        results: list[ReplayResult],
        interactions: list[RecordedInteraction],
        concurrency: int = 3,
    ) -> list[dict]:
        semaphore = asyncio.Semaphore(concurrency)
        interaction_map = {i.interaction_id: i for i in interactions}

        async def _judge(result):
            async with semaphore:
                interaction = interaction_map.get(result.interaction_id)
                user_message = ""
                if interaction and interaction.messages:
                    last_user = [m for m in interaction.messages if m.get("role") == "user"]
                    if last_user:
                        user_message = last_user[-1].get("content", "")
                return {
                    "interaction_id": result.interaction_id,
                    **await self.judge(result, user_message),
                }

        return await asyncio.gather(*[_judge(r) for r in results])
```

---

## Solution 5: Regression Test Suite Runner

Orchestrates the full replay + comparison pipeline and generates a structured report.

```python
from dataclasses import dataclass

@dataclass
class RegressionReport:
    run_id: str
    timestamp: float
    original_model: str
    candidate_model: str
    total_replayed: int
    verdicts: dict[str, int]
    regression_rate: float
    critical_regressions: list[str]   # interaction_ids
    sample_regressions: list[dict]
    passed: bool

class RegressionTestRunner:
    """Full pipeline: sample -> replay -> compare -> report."""

    def __init__(
        self,
        store: ReplayStore,
        replay_engine: ReplayEngine,
        comparator: SemanticSimilarityComparator,
        judge: Optional[LLMJudgeEvaluator] = None,
        regression_threshold: float = 0.05,  # Fail if >5% regressed
    ):
        self.store = store
        self.engine = replay_engine
        self.comparator = comparator
        self.judge = judge
        self.threshold = regression_threshold

    async def run(
        self,
        candidate_model: str,
        candidate_system_prompt: Optional[str] = None,
        sample_size: int = 200,
        tags: list[str] | None = None,
        use_judge: bool = False,
    ) -> RegressionReport:
        run_id = str(uuid.uuid4())[:8]
        print(f"[{run_id}] Sampling {sample_size} interactions...")
        interactions = await self.store.get_sample(sample_size, tags=tags)

        if not interactions:
            raise ValueError("No interactions in replay store")

        print(f"[{run_id}] Replaying {len(interactions)} interactions against {candidate_model}...")
        replay_results = await self.engine.replay_batch(
            interactions, candidate_model, candidate_system_prompt
        )

        print(f"[{run_id}] Comparing responses...")
        comparisons = await self.comparator.compare_batch(replay_results)

        verdicts: dict[str, int] = {"none": 0, "minor": 0, "major": 0, "critical": 0}
        critical_ids = []
        for comp in comparisons:
            verdicts[comp.regression_severity] += 1
            if comp.regression_severity == "critical":
                critical_ids.append(comp.interaction_id)

        if use_judge and self.judge:
            print(f"[{run_id}] Running LLM judge on {min(50, len(replay_results))} samples...")
            judge_results = await self.judge.judge_batch(
                replay_results[:50], interactions[:50]
            )
            # Re-classify critical from judge
            for jr in judge_results:
                if jr.get("verdict") == "CRITICAL_REGRESSION":
                    if jr["interaction_id"] not in critical_ids:
                        critical_ids.append(jr["interaction_id"])

        total = len(comparisons)
        regressed = verdicts["minor"] + verdicts["major"] + verdicts["critical"]
        regression_rate = regressed / total if total > 0 else 0.0
        passed = regression_rate <= self.threshold and len(critical_ids) == 0

        report = RegressionReport(
            run_id=run_id,
            timestamp=time.time(),
            original_model=interactions[0].model if interactions else "unknown",
            candidate_model=candidate_model,
            total_replayed=total,
            verdicts=verdicts,
            regression_rate=round(regression_rate, 4),
            critical_regressions=critical_ids,
            sample_regressions=[
                {"id": c.interaction_id, "score": c.similarity_score, "severity": c.regression_severity}
                for c in sorted(comparisons, key=lambda x: x.similarity_score)[:10]
            ],
            passed=passed,
        )

        print(f"[{run_id}] Result: {'PASS' if passed else 'FAIL'}")
        print(f"  Regression rate: {regression_rate:.1%} (threshold: {self.threshold:.1%})")
        print(f"  Verdicts: {verdicts}")
        return report
```

---

## Solution 6: CI Integration and Golden Set Management

Maintain a curated "golden set" of high-value replay interactions and integrate regression testing into CI/CD pipelines.

```python
import json
from pathlib import Path

class GoldenSetManager:
    """
    Manages a curated set of golden interactions for deterministic regression tests.
    Unlike sampled replay, golden sets are manually reviewed and hand-picked.
    """

    def __init__(self, golden_dir: str = "./golden_interactions"):
        self.dir = Path(golden_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save_golden(self, interaction: RecordedInteraction, name: str) -> None:
        """Pin an interaction as a golden test case."""
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(interaction.to_dict(), indent=2))

    def load_golden(self, name: str) -> RecordedInteraction:
        path = self.dir / f"{name}.json"
        data = json.loads(path.read_text())
        return RecordedInteraction(**data)

    def list_golden(self) -> list[str]:
        return [p.stem for p in self.dir.glob("*.json")]

    def load_all(self) -> list[RecordedInteraction]:
        return [self.load_golden(name) for name in self.list_golden()]


class CIRegressionGate:
    """
    Raises RegressionError if the candidate model fails the regression suite.
    Designed for use in CI pipelines (pytest, GitHub Actions, etc.).
    """

    def __init__(self, runner: RegressionTestRunner, golden_manager: GoldenSetManager):
        self.runner = runner
        self.golden = golden_manager

    async def assert_no_regression(
        self,
        candidate_model: str,
        candidate_prompt: Optional[str] = None,
        use_golden: bool = True,
        sample_size: int = 100,
    ) -> RegressionReport:
        if use_golden:
            # Load golden set into replay store
            for interaction in self.golden.load_all():
                await self.runner.store.save(interaction)

        report = await self.runner.run(
            candidate_model=candidate_model,
            candidate_system_prompt=candidate_prompt,
            sample_size=sample_size,
        )

        if not report.passed:
            raise AssertionError(
                f"Regression test FAILED for {candidate_model}: "
                f"regression_rate={report.regression_rate:.1%}, "
                f"critical={len(report.critical_regressions)}"
            )

        return report


# --- CI usage ---

async def run_ci_regression_check():
    """Entry point for CI pipeline."""
    store = ReplayStore()
    # ... configure replay_engine, comparator ...
    # runner = RegressionTestRunner(store, replay_engine, comparator)
    # gate = CIRegressionGate(runner, GoldenSetManager())
    # await gate.assert_no_regression("claude-3-5-sonnet-20241022")
    print("Regression gate would run here in CI")
```

---

## Comparison

| Solution | Automation | Coverage | Determinism | LLM Cost | Best For |
|---|---|---|---|---|---|
| Interaction Recorder | Passive capture | Production-wide | No | None | Building the replay corpus |
| Replay Engine | Active | Sampled | Yes (recorded tools) | None | Deterministic re-execution |
| Semantic Similarity | Automatic | Batch | N/A | None | Paraphrase-tolerant comparison |
| LLM-as-Judge | Automatic | Batch (limited) | N/A | Medium | Nuanced quality assessment |
| Regression Test Runner | Full pipeline | Configurable | Yes | Optional | Orchestrating the full suite |
| CI Gate + Golden Set | Automated CI | Curated | Yes | None | Blocking deploys on regressions |

**Start with the interaction recorder** to build a production corpus — even at 1% sample rate. **Use the semantic comparator** as the primary regression signal since it's free and scales well. **Add LLM-as-judge** for high-stakes model upgrades where subtle quality changes matter. **Curate a golden set** of 50–200 hand-picked interactions covering edge cases and past bugs. **Wire the CI gate** to block deploys automatically when regression rate exceeds threshold.
