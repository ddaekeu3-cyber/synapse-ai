---
title: "Agent Doesn't Implement Multi-Model Output Comparison Logging"
description: "Agents that switch between models (GPT-4o, Claude, Gemini) or upgrade model versions have no systematic way to compare output quality across models. Implement multi-model comparison logging to capture parallel outputs, score them, and make model selection decisions based on evidence."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-model-output-comparison-logging
tags: [multi-model, model-comparison, output-quality, observability, logging, model-selection]
symptoms:
  - "Model upgrade decisions are based on vibes rather than measured quality differences"
  - "No record of what each model returned for the same prompt across historical requests"
  - "Switching from GPT-4 to Claude caused silent quality regression with no detection"
  - "Cannot answer: 'which model performs better on our specific workload?'"
  - "A/B tests between models run in production with no logging infrastructure"
---

## Why This Happens

Multi-model deployments are common: fallback chains, A/B tests, shadow traffic, and model upgrades all involve routing the same prompt to different models. Without structured comparison logging, there is no dataset to analyze. Engineers rely on anecdotal feedback or periodic manual review. Systematic comparison logging captures both outputs for every comparison event, scores them, and aggregates statistics so model selection is data-driven.

## Solution 1: Parallel Model Comparison Runner

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ModelOutput:
    model_id: str
    content: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    finish_reason: str
    error: Optional[str] = None

@dataclass
class ComparisonRecord:
    comparison_id: str
    prompt_hash: str
    prompt_preview: str   # first 200 chars
    outputs: List[ModelOutput]
    winner: Optional[str]  # model_id of chosen output
    scores: Dict[str, float] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    session_id: Optional[str] = None
    task_type: Optional[str] = None

class ParallelModelComparer:
    """
    Runs the same prompt against multiple models concurrently.
    Records all outputs in a ComparisonRecord for analysis.
    """

    def __init__(self, model_clients: Dict[str, Any]):
        self._clients = model_clients  # {model_id: client}

    async def compare(
        self,
        prompt: str,
        model_ids: List[str],
        session_id: Optional[str] = None,
        task_type: Optional[str] = None,
        **kwargs,
    ) -> ComparisonRecord:
        import hashlib, uuid
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

        tasks = {
            model_id: asyncio.create_task(
                self._call_model(model_id, prompt, **kwargs)
            )
            for model_id in model_ids
            if model_id in self._clients
        }

        outputs = []
        for model_id, task in tasks.items():
            try:
                output = await task
                outputs.append(output)
            except Exception as exc:
                outputs.append(ModelOutput(
                    model_id=model_id, content="", input_tokens=0,
                    output_tokens=0, latency_ms=0.0,
                    finish_reason="error", error=str(exc),
                ))

        return ComparisonRecord(
            comparison_id=str(uuid.uuid4()),
            prompt_hash=prompt_hash,
            prompt_preview=prompt[:200],
            outputs=outputs,
            winner=None,
            session_id=session_id,
            task_type=task_type,
        )

    async def _call_model(self, model_id: str, prompt: str, **kwargs) -> ModelOutput:
        client = self._clients[model_id]
        t0 = time.monotonic()
        response = await client.complete(prompt, **kwargs)
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ModelOutput(
            model_id=model_id,
            content=response.content,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=elapsed_ms,
            finish_reason=response.finish_reason,
        )
```

## Solution 2: Output Scorer with Multiple Quality Dimensions

```python
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class QualityScores:
    model_id: str
    length_score: float          # normalized output length
    structure_score: float       # presence of requested formatting
    factual_keywords: float      # overlap with expected keywords
    instruction_follow: float    # does it match the requested format
    composite: float             # weighted average

class ModelOutputScorer:
    """
    Automatic scoring of model outputs on multiple quality dimensions.
    Complements human evaluation for high-volume logging.
    """

    WEIGHTS = {
        "length_score": 0.15,
        "structure_score": 0.25,
        "factual_keywords": 0.35,
        "instruction_follow": 0.25,
    }

    def score(
        self,
        output: ModelOutput,
        expected_keywords: Optional[List[str]] = None,
        expected_format: Optional[str] = None,  # "json" | "markdown" | "list"
    ) -> QualityScores:
        length = self._length_score(output.content)
        structure = self._structure_score(output.content, expected_format)
        keywords = self._keyword_score(output.content, expected_keywords or [])
        instruction = self._instruction_follow_score(output.content, expected_format)
        composite = (
            length * self.WEIGHTS["length_score"]
            + structure * self.WEIGHTS["structure_score"]
            + keywords * self.WEIGHTS["factual_keywords"]
            + instruction * self.WEIGHTS["instruction_follow"]
        )
        return QualityScores(
            model_id=output.model_id,
            length_score=length,
            structure_score=structure,
            factual_keywords=keywords,
            instruction_follow=instruction,
            composite=composite,
        )

    def _length_score(self, content: str, ideal_min: int = 100, ideal_max: int = 1000) -> float:
        n = len(content)
        if n < 10:
            return 0.0
        if n < ideal_min:
            return n / ideal_min
        if n > ideal_max:
            return max(0.0, 1.0 - (n - ideal_max) / ideal_max)
        return 1.0

    def _structure_score(self, content: str, fmt: Optional[str]) -> float:
        if fmt is None:
            return 0.5
        if fmt == "json":
            try:
                import json
                json.loads(content.strip())
                return 1.0
            except Exception:
                return 0.0
        if fmt == "markdown":
            has_headers = bool(re.search(r'^#{1,6}\s', content, re.MULTILINE))
            has_list = bool(re.search(r'^[\-\*]\s', content, re.MULTILINE))
            return 0.5 * has_headers + 0.5 * has_list
        if fmt == "list":
            items = re.findall(r'^\d+\.\s|^[\-\*]\s', content, re.MULTILINE)
            return min(len(items) / 3, 1.0)
        return 0.5

    def _keyword_score(self, content: str, keywords: List[str]) -> float:
        if not keywords:
            return 0.5
        content_lower = content.lower()
        found = sum(1 for kw in keywords if kw.lower() in content_lower)
        return found / len(keywords)

    def _instruction_follow_score(self, content: str, fmt: Optional[str]) -> float:
        if fmt == "json":
            return 1.0 if content.strip().startswith("{") or content.strip().startswith("[") else 0.0
        return 0.5

    def rank(self, outputs: List[ModelOutput], **kwargs) -> List[QualityScores]:
        scores = [self.score(o, **kwargs) for o in outputs if not o.error]
        return sorted(scores, key=lambda s: s.composite, reverse=True)
```

## Solution 3: Comparison Log Store with Query API

```python
import json
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

class ComparisonLogStore:
    """
    Persists ComparisonRecords for offline analysis.
    Provides query API to aggregate model performance by task type.
    """

    def __init__(self, db):
        self._db = db

    async def save(self, record: ComparisonRecord) -> None:
        for output in record.outputs:
            await self._db.execute(
                """
                INSERT INTO model_comparisons
                  (comparison_id, prompt_hash, prompt_preview, model_id, content_preview,
                   input_tokens, output_tokens, latency_ms, finish_reason, error,
                   score, winner, session_id, task_type, timestamp)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                record.comparison_id,
                record.prompt_hash,
                record.prompt_preview,
                output.model_id,
                output.content[:500],
                output.input_tokens,
                output.output_tokens,
                output.latency_ms,
                output.finish_reason,
                output.error,
                record.scores.get(output.model_id),
                record.winner,
                record.session_id,
                record.task_type,
                record.timestamp,
            )

    async def model_stats(
        self,
        task_type: Optional[str] = None,
        since_hours: float = 24.0,
    ) -> Dict[str, dict]:
        since = time.time() - since_hours * 3600
        where = "WHERE timestamp > $1"
        params = [since]
        if task_type:
            where += " AND task_type = $2"
            params.append(task_type)

        rows = await self._db.fetch(
            f"""
            SELECT model_id,
                   COUNT(*) AS calls,
                   AVG(latency_ms) AS avg_latency_ms,
                   AVG(output_tokens) AS avg_output_tokens,
                   AVG(score) AS avg_score,
                   SUM(CASE WHEN winner = model_id THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
            FROM model_comparisons {where}
            GROUP BY model_id
            """,
            *params,
        )
        return {
            r["model_id"]: {
                "calls": r["calls"],
                "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
                "avg_output_tokens": round(r["avg_output_tokens"] or 0, 1),
                "avg_score": round(r["avg_score"] or 0, 4),
                "win_rate": round((r["wins"] or 0) / max(r["calls"], 1), 4),
                "error_rate": round((r["errors"] or 0) / max(r["calls"], 1), 4),
            }
            for r in rows
        }
```

## Solution 4: Shadow Comparison (Production Model + Shadow Model)

```python
import asyncio
import time
from typing import Any, Callable

class ShadowModelLogger:
    """
    Runs the production model normally; runs the shadow model in the background.
    Returns production response to caller immediately; logs both for comparison.
    Does not block on shadow model latency.
    """

    def __init__(
        self,
        production_client,
        shadow_client,
        log_store: ComparisonLogStore,
        scorer: ModelOutputScorer,
        production_model_id: str = "production",
        shadow_model_id: str = "shadow",
    ):
        self._prod = production_client
        self._shadow = shadow_client
        self._store = log_store
        self._scorer = scorer
        self._prod_id = production_model_id
        self._shadow_id = shadow_model_id

    async def complete(self, prompt: str, session_id: str = "", task_type: str = "", **kwargs) -> Any:
        import uuid, hashlib

        # Run production model — caller waits for this
        t0 = time.monotonic()
        prod_response = await self._prod.complete(prompt, **kwargs)
        prod_latency = (time.monotonic() - t0) * 1000

        prod_output = ModelOutput(
            model_id=self._prod_id,
            content=prod_response.content,
            input_tokens=prod_response.usage.input_tokens,
            output_tokens=prod_response.usage.output_tokens,
            latency_ms=prod_latency,
            finish_reason=prod_response.finish_reason,
        )

        # Fire shadow call in background — does not block caller
        asyncio.create_task(
            self._shadow_and_log(prompt, prod_output, session_id, task_type, **kwargs)
        )

        return prod_response

    async def _shadow_and_log(
        self, prompt: str, prod_output: ModelOutput, session_id: str, task_type: str, **kwargs
    ) -> None:
        import uuid, hashlib
        try:
            t0 = time.monotonic()
            shadow_response = await self._shadow.complete(prompt, **kwargs)
            shadow_latency = (time.monotonic() - t0) * 1000

            shadow_output = ModelOutput(
                model_id=self._shadow_id,
                content=shadow_response.content,
                input_tokens=shadow_response.usage.input_tokens,
                output_tokens=shadow_response.usage.output_tokens,
                latency_ms=shadow_latency,
                finish_reason=shadow_response.finish_reason,
            )

            scores = self._scorer.rank([prod_output, shadow_output])
            score_map = {s.model_id: s.composite for s in scores}
            winner = scores[0].model_id if scores else self._prod_id

            record = ComparisonRecord(
                comparison_id=str(uuid.uuid4()),
                prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()[:16],
                prompt_preview=prompt[:200],
                outputs=[prod_output, shadow_output],
                winner=winner,
                scores=score_map,
                session_id=session_id,
                task_type=task_type,
            )
            await self._store.save(record)
        except Exception as exc:
            print(f"[shadow_model_logger] error: {exc}")
```

## Solution 5: Model Selection Advisor Based on Comparison History

```python
from typing import Dict, Optional

class ModelSelectionAdvisor:
    """
    Analyzes historical comparison data to recommend which model to use
    for a given task type, based on quality score and latency trade-off.
    """

    def __init__(self, log_store: ComparisonLogStore):
        self._store = log_store

    async def recommend(
        self,
        task_type: str,
        latency_budget_ms: Optional[float] = None,
        min_win_rate: float = 0.0,
    ) -> dict:
        stats = await self._store.model_stats(task_type=task_type, since_hours=168)  # 7 days
        if not stats:
            return {"recommendation": None, "reason": "insufficient_data"}

        candidates = [
            (model_id, s) for model_id, s in stats.items()
            if s["calls"] >= 50  # minimum sample size
            and s["error_rate"] < 0.05
            and s["win_rate"] >= min_win_rate
            and (latency_budget_ms is None or s["avg_latency_ms"] <= latency_budget_ms)
        ]

        if not candidates:
            return {"recommendation": None, "reason": "no_candidates_meet_criteria", "stats": stats}

        # Composite: 70% quality score, 30% inverse latency (normalized)
        max_latency = max(s["avg_latency_ms"] for _, s in candidates)
        scored = [
            (model_id, 0.7 * s["avg_score"] + 0.3 * (1 - s["avg_latency_ms"] / max(max_latency, 1)))
            for model_id, s in candidates
        ]
        best_model, best_score = max(scored, key=lambda x: x[1])

        return {
            "recommendation": best_model,
            "composite_score": round(best_score, 4),
            "reason": f"Highest quality+latency composite over {stats[best_model]['calls']} samples",
            "stats": stats,
        }
```

## Solution 6: Comparison Dashboard Reporter

```python
import time
from typing import List, Optional

class ModelComparisonDashboard:
    def __init__(self, store: ComparisonLogStore, advisor: ModelSelectionAdvisor):
        self._store = store
        self._advisor = advisor

    async def daily_report(self) -> dict:
        stats = await self._store.model_stats(since_hours=24)
        task_types = ["qa", "summarization", "code_generation", "classification"]
        recommendations = {}
        for task in task_types:
            rec = await self._advisor.recommend(task)
            if rec["recommendation"]:
                recommendations[task] = rec["recommendation"]

        return {
            "generated_at": time.time(),
            "model_stats_24h": stats,
            "recommendations": recommendations,
            "summary": self._summarize(stats),
        }

    def _summarize(self, stats: dict) -> str:
        if not stats:
            return "No comparison data available."
        lines = []
        for model_id, s in sorted(stats.items(), key=lambda x: x[1]["avg_score"], reverse=True):
            lines.append(
                f"  {model_id}: calls={s['calls']} score={s['avg_score']:.3f} "
                f"win_rate={s['win_rate']:.1%} latency={s['avg_latency_ms']:.0f}ms "
                f"errors={s['error_rate']:.1%}"
            )
        return "\n".join(lines)

    async def print_report(self) -> None:
        report = await self.daily_report()
        print(f"\n=== Model Comparison Report (24h) ===")
        print(report["summary"])
        if report["recommendations"]:
            print("\nRecommendations by task:")
            for task, model in report["recommendations"].items():
                print(f"  {task}: {model}")
```

## Comparison

| Approach | Parallel Execution | Automatic Scoring | Production-Safe | Historical Analysis |
|---|---|---|---|---|
| ParallelModelComparer | Yes | No | No (both wait) | Via log store |
| ModelOutputScorer | N/A | Yes (multi-dim) | N/A | Via scored records |
| ComparisonLogStore | N/A | N/A | N/A | Yes (SQL aggregation) |
| ShadowModelLogger | Yes (shadow async) | Yes (scorer) | Yes (non-blocking) | Via log store |
| ModelSelectionAdvisor | N/A | N/A | N/A | Yes (data-driven recommendation) |
| ModelComparisonDashboard | N/A | Via scorer | N/A | Yes (daily report) |

**Best for production**: Use `ShadowModelLogger` for zero-latency-impact comparison logging (production path is unblocked). Use `ModelOutputScorer` for automatic quality scoring. Store results in `ComparisonLogStore` and run `ModelSelectionAdvisor` weekly to data-drive model upgrade decisions.
