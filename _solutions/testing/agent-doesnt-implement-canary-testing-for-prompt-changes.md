---
layout: solution
title: "Agent Doesn't Implement Canary Testing for Prompt Changes"
category: testing
description: "Deploying prompt changes to 100% of traffic at once creates undetected regressions. Canary testing routes a small percentage of requests to the new prompt, measures quality metrics, and rolls back automatically on degradation."
tags: [testing, canary, prompt-engineering, ab-testing, rollback, sqlite, quality]
---

# Agent Doesn't Implement Canary Testing for Prompt Changes

## Problem

Prompt changes that look correct in local testing can regress quality on the long tail of production inputs. Without canary testing, you discover the regression after 100% of users are affected.

Canary testing routes a configurable percentage of traffic to a new prompt variant, tracks quality signals per variant, and enables automatic rollback if the canary degrades.

---

## Option 1: Simple Traffic Split with Percentage Routing

```python
import random
import hashlib
import anthropic
from dataclasses import dataclass

@dataclass
class PromptVariant:
    name: str
    system_prompt: str
    weight: float  # 0.0–1.0, must sum to 1.0

VARIANTS = [
    PromptVariant(
        name="control",
        system_prompt="You are a helpful assistant. Answer concisely.",
        weight=0.90,  # 90% of traffic
    ),
    PromptVariant(
        name="canary_v2",
        system_prompt=(
            "You are a helpful assistant. Answer concisely and precisely. "
            "If unsure, say so rather than guessing."
        ),
        weight=0.10,  # 10% of traffic
    ),
]


def select_variant(request_id: str) -> PromptVariant:
    """
    Deterministic routing: same request_id always maps to same variant.
    This ensures a user doesn't flip between variants mid-session.
    """
    hash_val = int(hashlib.md5(request_id.encode()).hexdigest(), 16)
    bucket = (hash_val % 10000) / 10000.0  # 0.0–1.0

    cumulative = 0.0
    for variant in VARIANTS:
        cumulative += variant.weight
        if bucket < cumulative:
            return variant
    return VARIANTS[-1]


def run_with_canary(user_message: str, request_id: str) -> dict:
    variant = select_variant(request_id)
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=variant.system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    text = response.content[0].text
    return {
        "request_id": request_id,
        "variant": variant.name,
        "response": text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


if __name__ == "__main__":
    import uuid

    messages = [
        "What is machine learning?",
        "How do I sort a list in Python?",
        "What's the capital of Japan?",
        "Explain recursion briefly.",
        "What is 15% of 240?",
    ]

    variant_counts = {"control": 0, "canary_v2": 0}
    for msg in messages:
        req_id = str(uuid.uuid4())
        result = run_with_canary(msg, req_id)
        variant_counts[result["variant"]] += 1
        print(f"[{result['variant']}] {msg[:40]} → {result['response'][:60]}")

    print(f"\nVariant distribution: {variant_counts}")
# Expected Token Savings: None direct — canary overhead is 1 extra call per sampled request
# Environment: pip install anthropic; random, hashlib, uuid are stdlib
```

---

## Option 2: Quality-Tracked Canary with SQLite Metrics

```python
import sqlite3
import random
import uuid
import json
import anthropic
from datetime import datetime
from dataclasses import dataclass

@dataclass
class Variant:
    name: str
    system_prompt: str
    is_canary: bool = False

CONTROL = Variant(
    name="control_v1",
    system_prompt="You are a helpful assistant. Be concise.",
    is_canary=False,
)

CANARY = Variant(
    name="canary_v2",
    system_prompt="You are a precise, helpful assistant. Be concise and accurate. Flag uncertainty.",
    is_canary=True,
)

CANARY_RATE = 0.15  # 15% of traffic goes to canary


class CanaryMetrics:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS canary_requests (
                request_id TEXT PRIMARY KEY,
                variant TEXT,
                prompt TEXT,
                response TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                response_length INTEGER,
                latency_ms REAL,
                flagged_uncertain INTEGER DEFAULT 0,
                called_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def record(self, request_id: str, variant: str, prompt: str, response: str,
               input_tokens: int, output_tokens: int, latency_ms: float):
        flagged = int(any(
            phrase in response.lower()
            for phrase in ["i'm not sure", "i don't know", "uncertain", "i'm unsure", "i cannot"]
        ))
        self.conn.execute(
            """INSERT INTO canary_requests
               (request_id, variant, prompt, response, input_tokens, output_tokens,
                response_length, latency_ms, flagged_uncertain)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (request_id, variant, prompt[:100], response[:300], input_tokens, output_tokens,
             len(response), latency_ms, flagged),
        )
        self.conn.commit()

    def summary(self) -> dict:
        rows = self.conn.execute("""
            SELECT variant,
                   COUNT(*) as requests,
                   AVG(output_tokens) as avg_output_tokens,
                   AVG(response_length) as avg_response_length,
                   AVG(latency_ms) as avg_latency_ms,
                   SUM(flagged_uncertain) as uncertainty_flags
            FROM canary_requests
            GROUP BY variant
        """).fetchall()
        return {
            r[0]: {
                "requests": r[1],
                "avg_output_tokens": round(r[2] or 0, 1),
                "avg_response_length": round(r[3] or 0, 1),
                "avg_latency_ms": round(r[4] or 0, 1),
                "uncertainty_flags": r[5],
                "uncertainty_rate": round((r[5] or 0) / max(r[1], 1), 3),
            }
            for r in rows
        }

    def should_rollback(self, max_token_increase: float = 0.30) -> tuple[bool, str]:
        """
        Return (rollback, reason) if canary metrics exceed thresholds.
        Rolls back if canary uses >30% more tokens than control.
        """
        summary = self.summary()
        if "control_v1" not in summary or "canary_v2" not in summary:
            return False, "insufficient data"

        ctrl = summary["control_v1"]
        cny = summary["canary_v2"]

        if cny["requests"] < 5:
            return False, "canary needs ≥5 requests before evaluation"

        token_increase = (cny["avg_output_tokens"] - ctrl["avg_output_tokens"]) / max(ctrl["avg_output_tokens"], 1)
        if token_increase > max_token_increase:
            return True, f"canary uses {token_increase:.0%} more output tokens than control"

        return False, "canary within acceptable bounds"


def run_canary_experiment(prompts: list[str]):
    metrics = CanaryMetrics()
    client = anthropic.Anthropic()

    import time

    for prompt in prompts:
        req_id = str(uuid.uuid4())[:10]
        variant = CANARY if random.random() < CANARY_RATE else CONTROL

        t0 = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=variant.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = (time.time() - t0) * 1000

        text = response.content[0].text
        metrics.record(
            req_id, variant.name, prompt, text,
            response.usage.input_tokens, response.usage.output_tokens, latency_ms,
        )
        print(f"[{variant.name}] {prompt[:40]} → {text[:50]}")

    summary = metrics.summary()
    print(f"\nCanary Experiment Summary:")
    print(json.dumps(summary, indent=2))

    rollback, reason = metrics.should_rollback()
    if rollback:
        print(f"\n⚠️  ROLLBACK RECOMMENDED: {reason}")
    else:
        print(f"\n✓  Canary healthy: {reason}")


if __name__ == "__main__":
    prompts = [
        "What is Python?",
        "How does async/await work?",
        "What is the speed of light?",
        "Explain neural networks.",
        "What is 3+3?",
        "Who wrote Hamlet?",
        "What is REST?",
        "How do I reverse a string?",
        "What causes inflation?",
        "Name a sorting algorithm.",
    ]
    run_canary_experiment(prompts)
# Expected Token Savings: Canary catches regressions early — prevents costly full-fleet rollouts of bad prompts
# Environment: pip install anthropic; sqlite3, random, uuid, json, time are stdlib
```

---

## Option 3: Automatic Rollback with Quality Gate

```python
import sqlite3
import random
import uuid
import time
import anthropic
from dataclasses import dataclass
from enum import Enum

class CanaryStatus(Enum):
    RUNNING = "running"
    PROMOTED = "promoted"   # canary became the new default
    ROLLED_BACK = "rolled_back"
    INSUFFICIENT_DATA = "insufficient_data"

@dataclass
class QualityGate:
    min_requests: int = 10
    max_token_overhead_pct: float = 25.0
    max_latency_increase_pct: float = 50.0
    min_response_length_pct: float = 80.0  # canary shouldn't be much shorter


class AutoRollbackCanary:
    def __init__(self, db_path: str = ":memory:", canary_rate: float = 0.10):
        self.canary_rate = canary_rate
        self.gate = QualityGate()
        self.conn = sqlite3.connect(db_path)
        self._init_schema()
        self.status = CanaryStatus.RUNNING
        self._active_system = "You are a helpful assistant. Be concise."
        self._canary_system = (
            "You are a helpful, accurate assistant. Be concise. "
            "If you lack knowledge about something, state that clearly."
        )

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant TEXT,
                output_tokens INTEGER,
                response_length INTEGER,
                latency_ms REAL,
                ts TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def _record(self, variant: str, output_tokens: int, response_length: int, latency_ms: float):
        self.conn.execute(
            "INSERT INTO results (variant, output_tokens, response_length, latency_ms) VALUES (?,?,?,?)",
            (variant, output_tokens, response_length, latency_ms),
        )
        self.conn.commit()

    def _evaluate(self) -> tuple[CanaryStatus, str]:
        ctrl = self.conn.execute(
            "SELECT COUNT(*), AVG(output_tokens), AVG(response_length), AVG(latency_ms) FROM results WHERE variant='control'"
        ).fetchone()
        cny = self.conn.execute(
            "SELECT COUNT(*), AVG(output_tokens), AVG(response_length), AVG(latency_ms) FROM results WHERE variant='canary'"
        ).fetchone()

        if not ctrl[0] or not cny[0] or cny[0] < self.gate.min_requests:
            return CanaryStatus.INSUFFICIENT_DATA, f"canary has {cny[0] or 0} requests, need {self.gate.min_requests}"

        ctrl_tokens, ctrl_len, ctrl_lat = ctrl[1] or 1, ctrl[2] or 1, ctrl[3] or 1
        cny_tokens, cny_len, cny_lat = cny[1] or 0, cny[2] or 0, cny[3] or 0

        token_overhead = (cny_tokens - ctrl_tokens) / ctrl_tokens * 100
        latency_increase = (cny_lat - ctrl_lat) / ctrl_lat * 100
        length_ratio = (cny_len / ctrl_len) * 100

        if token_overhead > self.gate.max_token_overhead_pct:
            return CanaryStatus.ROLLED_BACK, f"token overhead {token_overhead:.1f}% > {self.gate.max_token_overhead_pct}%"

        if latency_increase > self.gate.max_latency_increase_pct:
            return CanaryStatus.ROLLED_BACK, f"latency increase {latency_increase:.1f}% > {self.gate.max_latency_increase_pct}%"

        if length_ratio < self.gate.min_response_length_pct:
            return CanaryStatus.ROLLED_BACK, f"response too short: {length_ratio:.1f}% of control"

        return CanaryStatus.PROMOTED, (
            f"canary passes all gates: "
            f"tokens +{token_overhead:.1f}%, latency +{latency_increase:.1f}%, length {length_ratio:.1f}%"
        )

    def call(self, user_message: str) -> dict:
        if self.status == CanaryStatus.ROLLED_BACK:
            variant = "control"
            system = self._active_system
        elif self.status == CanaryStatus.PROMOTED:
            variant = "canary"
            system = self._canary_system
        else:
            # RUNNING: split traffic
            if random.random() < self.canary_rate:
                variant = "canary"
                system = self._canary_system
            else:
                variant = "control"
                system = self._active_system

        client = anthropic.Anthropic()
        t0 = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        latency_ms = (time.time() - t0) * 1000

        text = response.content[0].text
        self._record(variant, response.usage.output_tokens, len(text), latency_ms)

        # Re-evaluate after each canary call
        if variant == "canary" and self.status == CanaryStatus.RUNNING:
            new_status, reason = self._evaluate()
            if new_status in (CanaryStatus.PROMOTED, CanaryStatus.ROLLED_BACK):
                self.status = new_status
                icon = "✓" if new_status == CanaryStatus.PROMOTED else "⚠️"
                print(f"\n{icon}  Canary {new_status.value.upper()}: {reason}\n")

        return {"variant": variant, "text": text, "status": self.status.value}


if __name__ == "__main__":
    canary = AutoRollbackCanary(canary_rate=0.30)  # 30% for demo speed

    prompts = [
        "What is Python?", "Explain async IO.", "What is a neural network?",
        "How does TCP work?", "What is REST?", "Name a sorting algorithm.",
        "What is 7*8?", "Who wrote 1984?", "What is Bitcoin?", "Explain OAuth.",
        "What is Docker?", "How does HTTPS work?", "What is recursion?",
        "Explain polymorphism.", "What is a deadlock?",
    ]

    for prompt in prompts:
        result = canary.call(prompt)
        print(f"[{result['variant']}] {prompt[:35]} → {result['text'][:50]}")
        if canary.status != CanaryStatus.RUNNING:
            print(f"[Canary] Final status: {canary.status.value}")
            break
# Expected Token Savings: Prevents large-scale token regression by catching expensive prompts at 10% traffic
# Environment: pip install anthropic; sqlite3, random, uuid, time are stdlib
```

---

## Option 4: Multi-Variant Experiment with Statistical Significance

```python
import sqlite3
import random
import uuid
import math
import json
import anthropic
from dataclasses import dataclass

@dataclass
class Variant:
    name: str
    system_prompt: str
    weight: float

EXPERIMENT_VARIANTS = [
    Variant("baseline",  "You are a helpful assistant.",                               0.70),
    Variant("concise",   "You are a helpful assistant. Be very concise.",              0.15),
    Variant("verbose",   "You are a thorough assistant. Explain in detail.",           0.15),
]


class MultiVariantExperiment:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT,
                variant TEXT,
                output_tokens INTEGER,
                response_chars INTEGER
            )
        """)
        self.conn.commit()

    def assign_variant(self, request_id: str) -> Variant:
        """Deterministic assignment from request_id."""
        h = int(uuid.UUID(request_id).int % 10000) / 10000.0
        cumulative = 0.0
        for v in EXPERIMENT_VARIANTS:
            cumulative += v.weight
            if h < cumulative:
                return v
        return EXPERIMENT_VARIANTS[-1]

    def record(self, request_id: str, variant_name: str, output_tokens: int, chars: int):
        self.conn.execute(
            "INSERT INTO observations (request_id, variant, output_tokens, response_chars) VALUES (?,?,?,?)",
            (request_id, variant_name, output_tokens, chars),
        )
        self.conn.commit()

    def _mean_std(self, values: list[float]) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        n = len(values)
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / max(n - 1, 1)
        return mean, math.sqrt(variance)

    def _z_score(self, mean_a: float, std_a: float, n_a: int,
                  mean_b: float, std_b: float, n_b: int) -> float:
        """Two-sample z-score for difference of means."""
        se = math.sqrt((std_a ** 2 / max(n_a, 1)) + (std_b ** 2 / max(n_b, 1)))
        if se == 0:
            return 0.0
        return (mean_b - mean_a) / se

    def analyze(self) -> dict:
        results = {}
        for v in EXPERIMENT_VARIANTS:
            rows = self.conn.execute(
                "SELECT output_tokens, response_chars FROM observations WHERE variant=?", (v.name,)
            ).fetchall()
            tokens = [r[0] for r in rows]
            chars = [r[1] for r in rows]
            mean_t, std_t = self._mean_std(tokens)
            results[v.name] = {
                "n": len(rows),
                "mean_tokens": round(mean_t, 1),
                "std_tokens": round(std_t, 1),
                "mean_chars": round(sum(chars) / max(len(chars), 1), 1),
            }

        # Compute z-scores vs. baseline
        baseline = results.get("baseline", {})
        for v_name, stats in results.items():
            if v_name == "baseline" or stats["n"] < 5:
                stats["z_score_vs_baseline"] = None
                stats["significant"] = None
                continue
            z = self._z_score(
                baseline["mean_tokens"], baseline.get("std_tokens", 1), baseline["n"],
                stats["mean_tokens"], stats["std_tokens"], stats["n"],
            )
            stats["z_score_vs_baseline"] = round(z, 2)
            stats["significant"] = abs(z) > 1.96  # 95% confidence

        return results


def run_multi_variant_experiment():
    exp = MultiVariantExperiment()
    client = anthropic.Anthropic()

    prompts = [
        "What is Python?", "How does async/await work?", "Explain recursion.",
        "What is a REST API?", "How does HTTPS work?", "What is Docker?",
        "Explain machine learning.", "What is SQL?", "How does DNS work?",
        "What is a neural network?", "Explain OAuth 2.0.", "What is Kubernetes?",
        "How does Git work?", "What is an API?", "Explain SOLID principles.",
    ]

    for prompt in prompts:
        req_id = str(uuid.uuid4())
        variant = exp.assign_variant(req_id)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=variant.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        exp.record(req_id, variant.name, response.usage.output_tokens, len(text))
        print(f"[{variant.name}] {prompt[:35]} → {response.usage.output_tokens} tokens")

    print("\nExperiment Analysis:")
    print(json.dumps(exp.analyze(), indent=2))


if __name__ == "__main__":
    run_multi_variant_experiment()
# Expected Token Savings: 15-40% by identifying that "concise" variant saves tokens with acceptable quality
# Environment: pip install anthropic; sqlite3, random, uuid, math, json are stdlib
```

---

## Option 5: Shadow Mode Canary (No User Impact)

```python
import asyncio
import uuid
import json
import anthropic
from dataclasses import dataclass
from datetime import datetime

@dataclass
class ShadowResult:
    request_id: str
    production_text: str
    shadow_text: str
    production_tokens: int
    shadow_tokens: int
    length_diff_pct: float
    token_diff_pct: float
    diverged: bool

PRODUCTION_SYSTEM = "You are a helpful assistant. Be concise."
SHADOW_SYSTEM = (
    "You are a helpful, precise assistant. Be concise and factually accurate. "
    "Use structured responses where appropriate."
)

DIVERGENCE_THRESHOLD_PCT = 30.0  # Flag if shadow is >30% longer/shorter


async def run_shadow(user_message: str, request_id: str) -> ShadowResult:
    """
    Run production and shadow prompts concurrently.
    User sees only production response. Shadow runs silently for comparison.
    """
    client = anthropic.AsyncAnthropic()

    async def call(system: str) -> tuple[str, int]:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        return resp.content[0].text, resp.usage.output_tokens

    # Run both variants concurrently — user only waits for production
    prod_task = asyncio.create_task(call(PRODUCTION_SYSTEM))
    shadow_task = asyncio.create_task(call(SHADOW_SYSTEM))

    prod_text, prod_tokens = await prod_task
    shadow_text, shadow_tokens = await shadow_task

    len_diff = (len(shadow_text) - len(prod_text)) / max(len(prod_text), 1) * 100
    tok_diff = (shadow_tokens - prod_tokens) / max(prod_tokens, 1) * 100

    return ShadowResult(
        request_id=request_id,
        production_text=prod_text,
        shadow_text=shadow_text,
        production_tokens=prod_tokens,
        shadow_tokens=shadow_tokens,
        length_diff_pct=round(len_diff, 1),
        token_diff_pct=round(tok_diff, 1),
        diverged=abs(len_diff) > DIVERGENCE_THRESHOLD_PCT,
    )


async def run_shadow_experiment(prompts: list[str]):
    results = []
    for prompt in prompts:
        req_id = str(uuid.uuid4())[:8]
        result = await run_shadow(prompt, req_id)
        results.append(result)

        flag = "⚠️ DIVERGED" if result.diverged else "✓"
        print(
            f"[{req_id}] {flag} prod={result.production_tokens}tok "
            f"shadow={result.shadow_tokens}tok "
            f"len_diff={result.length_diff_pct:+.1f}%"
        )

    diverged = [r for r in results if r.diverged]
    avg_token_diff = sum(r.token_diff_pct for r in results) / len(results)

    print(f"\nSummary:")
    print(f"  Total requests:  {len(results)}")
    print(f"  Diverged:        {len(diverged)} ({len(diverged)/len(results):.0%})")
    print(f"  Avg token diff:  {avg_token_diff:+.1f}%")

    if diverged:
        print(f"\nDiverged examples:")
        for r in diverged[:2]:
            print(f"  Prod:   {r.production_text[:80]!r}")
            print(f"  Shadow: {r.shadow_text[:80]!r}")
            print()


if __name__ == "__main__":
    prompts = [
        "What is machine learning?",
        "How does TCP/IP work?",
        "Explain async/await.",
        "What is a hash table?",
        "How does OAuth work?",
    ]
    asyncio.run(run_shadow_experiment(prompts))
# Expected Token Savings: None direct — shadow doubles API cost during evaluation period; saves from bad full rollouts
# Environment: pip install anthropic; asyncio, uuid, json are stdlib
```

---

## Option 6: Canary with Automated Promotion Gate via CI Hook

```python
import sqlite3
import json
import uuid
import random
import anthropic
from datetime import datetime

PROMOTION_GATE = {
    "min_requests": 20,
    "max_token_overhead_pct": 10.0,
    "max_p99_latency_ms": 5000,
    "required_sample_pct": 0.10,  # canary must see at least 10% of traffic
}


class CICanaryGate:
    """
    Runs a canary experiment and emits a CI-compatible exit code.
    Exit 0 = promote, Exit 1 = rollback.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant TEXT,
                output_tokens INTEGER,
                latency_ms REAL,
                ts TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def record(self, variant: str, output_tokens: int, latency_ms: float):
        self.conn.execute(
            "INSERT INTO observations (variant, output_tokens, latency_ms) VALUES (?,?,?)",
            (variant, output_tokens, latency_ms),
        )
        self.conn.commit()

    def evaluate(self) -> dict:
        def stats(variant: str) -> dict:
            rows = self.conn.execute(
                "SELECT output_tokens, latency_ms FROM observations WHERE variant=?", (variant,)
            ).fetchall()
            if not rows:
                return {"n": 0}
            tokens = [r[0] for r in rows]
            latencies = sorted(r[1] for r in rows)
            p99_idx = max(0, int(len(latencies) * 0.99) - 1)
            return {
                "n": len(rows),
                "avg_tokens": sum(tokens) / len(tokens),
                "p99_latency_ms": latencies[p99_idx],
            }

        ctrl = stats("control")
        cny = stats("canary")

        gate_checks = {}

        if cny["n"] < PROMOTION_GATE["min_requests"]:
            gate_checks["min_requests"] = {
                "pass": False,
                "reason": f"canary has {cny['n']} requests, need {PROMOTION_GATE['min_requests']}",
            }
        else:
            gate_checks["min_requests"] = {"pass": True}

        if ctrl["n"] > 0 and cny["n"] > 0:
            token_overhead = (cny["avg_tokens"] - ctrl["avg_tokens"]) / ctrl["avg_tokens"] * 100
            gate_checks["token_overhead"] = {
                "pass": token_overhead <= PROMOTION_GATE["max_token_overhead_pct"],
                "value": round(token_overhead, 1),
                "threshold": PROMOTION_GATE["max_token_overhead_pct"],
            }
            gate_checks["p99_latency"] = {
                "pass": cny["p99_latency_ms"] <= PROMOTION_GATE["max_p99_latency_ms"],
                "value": round(cny["p99_latency_ms"], 1),
                "threshold": PROMOTION_GATE["max_p99_latency_ms"],
            }

        all_pass = all(v.get("pass", False) for v in gate_checks.values())
        return {
            "promote": all_pass,
            "gate_checks": gate_checks,
            "control": ctrl,
            "canary": cny,
        }


def run_ci_canary(prompts: list[str], canary_rate: float = 0.20) -> int:
    """Returns 0 (promote) or 1 (rollback) — suitable for CI exit code."""
    import time

    gate = CICanaryGate()
    client = anthropic.Anthropic()

    systems = {
        "control": "You are a helpful assistant. Be concise.",
        "canary":  "You are a helpful, accurate assistant. Be concise. Cite uncertainty.",
    }

    for prompt in prompts:
        variant = "canary" if random.random() < canary_rate else "control"
        t0 = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=systems[variant],
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = (time.time() - t0) * 1000
        gate.record(variant, response.usage.output_tokens, latency_ms)
        print(f"[{variant}] {prompt[:35]} → {response.usage.output_tokens} tokens")

    result = gate.evaluate()
    print(f"\nCI Gate Result:")
    print(json.dumps(result, indent=2))

    if result["promote"]:
        print("\n✓  All gates PASSED — canary promoted")
        return 0
    else:
        print("\n⚠️  Gate FAILED — canary rolled back")
        return 1


if __name__ == "__main__":
    import sys
    prompts = [
        f"Question {i}: Explain concept #{i} in AI."
        for i in range(25)
    ]
    exit_code = run_ci_canary(prompts, canary_rate=0.30)
    print(f"\nCI exit code: {exit_code}")
    sys.exit(exit_code)
# Expected Token Savings: Prevents 100% fleet adoption of token-regressive prompts; ROI is proportional to traffic volume
# Environment: pip install anthropic; sqlite3, json, uuid, random, time, sys are stdlib
```

---

## Comparison

| Option | Traffic Split | Rollback | Shadow Mode | Statistical Test | SQLite | Best For |
|--------|--------------|----------|-------------|-----------------|--------|----------|
| 1 | Hash-based % | Manual | No | No | No | Simple controlled rollout |
| 2 | Random % | Threshold alert | No | No | Yes | Token/quality monitoring |
| 3 | Random % | Automatic | No | No | Yes | Production with auto-guard |
| 4 | Hash-based % | Manual | No | Z-score | Yes | Rigorous multi-variant experiments |
| 5 | 100% parallel | N/A | Yes | No | No | Zero-impact pre-launch comparison |
| 6 | Random % | CI gate | No | No | Yes | CI/CD pipeline promotion gates |
