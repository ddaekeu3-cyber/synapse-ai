---
layout: solution
title: "Agent Doesn't Implement Canary Deployment for Model Version Updates"
category: general
description: "Agent ships model version changes to 100% of traffic immediately — exposing all users to potential quality regressions when a new model behaves differently, with no safe rollback path."
tags: [general, canary, deployment, model-versioning, rollback, a-b-testing, reliability]
---

# Agent Doesn't Implement Canary Deployment for Model Version Updates

## Problem

When a new Claude model is released (or when switching model parameters), agents typically update their code and deploy to all users at once. If the new model behaves differently — different output format, changed reasoning patterns, unexpected refusals — every user is affected immediately with no easy rollback.

**Root cause:** Model version is a hardcoded string; there's no traffic-splitting or gradual rollout mechanism between model versions.

**Symptoms:**
- A model update that changes JSON output format breaks downstream parsing for all users simultaneously
- A new model version is more conservative, refusing edge-case prompts that the previous model handled
- Rollback requires a full code deployment rather than a config change
- No data on how the new model performs before full rollout

---

## Option 1: Weighted Random Traffic Split Between Model Versions

Route a percentage of traffic to the new model; monitor outcomes before increasing the percentage.

```python
import anthropic
import json
import random
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass, field

client = anthropic.Anthropic()
CANARY_DB = Path("/tmp/canary_metrics.db")

def init_canary_db() -> sqlite3.Connection:
    conn = sqlite3.connect(CANARY_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            request_id TEXT,
            latency_ms INTEGER,
            output_tokens INTEGER,
            success INTEGER DEFAULT 1,
            error_type TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

@dataclass
class ModelCanaryConfig:
    stable_model: str = "claude-haiku-4-5-20251001"
    canary_model: str = "claude-sonnet-4-6"   # New model being tested
    canary_traffic_pct: float = 0.10           # 10% to canary
    min_samples_before_promote: int = 50
    max_error_rate_pct: float = 5.0            # Auto-rollback if error rate exceeds this

config = ModelCanaryConfig()
conn = init_canary_db()

def select_model(config: ModelCanaryConfig) -> str:
    """Route traffic based on canary percentage."""
    return config.canary_model if random.random() < config.canary_traffic_pct else config.stable_model

def record_metric(conn, model: str, latency_ms: int, tokens: int, success: bool, error: str = ""):
    conn.execute(
        "INSERT INTO model_metrics (model, latency_ms, output_tokens, success, error_type) VALUES (?, ?, ?, ?, ?)",
        (model, latency_ms, tokens, int(success), error)
    )
    conn.commit()

def get_canary_stats(conn, model: str, window_hours: int = 1) -> dict:
    rows = conn.execute("""
        SELECT COUNT(*), SUM(success), AVG(latency_ms), AVG(output_tokens)
        FROM model_metrics
        WHERE model=? AND created_at >= datetime('now', ?)
    """, (model, f"-{window_hours} hours")).fetchone()
    total, successes, avg_latency, avg_tokens = rows
    if not total:
        return {"total": 0, "success_rate": 1.0, "error_rate": 0.0, "avg_latency_ms": 0}
    return {
        "total": total,
        "success_rate": (successes or 0) / total,
        "error_rate": (1 - (successes or 0) / total) * 100,
        "avg_latency_ms": round(avg_latency or 0),
        "avg_tokens": round(avg_tokens or 0)
    }

def should_rollback(conn, config: ModelCanaryConfig) -> tuple[bool, str]:
    stats = get_canary_stats(conn, config.canary_model)
    if stats["total"] < 5:
        return False, "Insufficient data"
    if stats["error_rate"] > config.max_error_rate_pct:
        return True, f"Error rate {stats['error_rate']:.1f}% > threshold {config.max_error_rate_pct}%"
    return False, "Healthy"

def canary_model_call(user_query: str, config: ModelCanaryConfig) -> dict:
    model = select_model(config)
    start = time.time()
    rollback, reason = should_rollback(conn, config)
    if rollback:
        print(f"[canary] AUTO-ROLLBACK: {reason}. Using stable model.")
        model = config.stable_model

    try:
        response = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": user_query}]
        )
        latency = round((time.time() - start) * 1000)
        tokens = response.usage.output_tokens
        record_metric(conn, model, latency, tokens, True)
        is_canary = model == config.canary_model
        return {
            "answer": response.content[0].text,
            "model_used": model,
            "is_canary": is_canary,
            "latency_ms": latency
        }
    except Exception as e:
        latency = round((time.time() - start) * 1000)
        record_metric(conn, model, latency, 0, False, type(e).__name__)
        raise

# Simulate traffic
for i in range(20):
    result = canary_model_call(f"Explain topic {i % 5}", config)
    model_tag = "[CANARY]" if result["is_canary"] else "[stable]"
    print(f"{model_tag} {result['model_used']}: {result['answer'][:50]}...")

# Print stats
for model in [config.stable_model, config.canary_model]:
    stats = get_canary_stats(conn, model)
    print(f"\n[stats] {model}: n={stats['total']}, err={stats['error_rate']:.1f}%, latency={stats['avg_latency_ms']}ms")

# Expected Token Savings: ~0% (canary is routing logic; doesn't change token count)
# Environment: Production agents where model updates require safe rollout; high-stakes customer-facing systems
```

---

## Option 2: Sticky Canary — Consistent Model Assignment Per User

Assign each user deterministically to a model cohort so their experience is consistent across requests.

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from pathlib import Path

client = anthropic.Anthropic()
STICKY_DB = Path("/tmp/sticky_canary.db")

def init_sticky_db() -> sqlite3.Connection:
    conn = sqlite3.connect(STICKY_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_assignments (
            user_id TEXT PRIMARY KEY,
            assigned_model TEXT NOT NULL,
            assigned_at TEXT DEFAULT (datetime('now')),
            request_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canary_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # Default config
    conn.execute("INSERT OR IGNORE INTO canary_config (key, value) VALUES ('canary_pct', '0.10')")
    conn.execute("INSERT OR IGNORE INTO canary_config (key, value) VALUES ('stable_model', 'claude-haiku-4-5-20251001')")
    conn.execute("INSERT OR IGNORE INTO canary_config (key, value) VALUES ('canary_model', 'claude-haiku-4-5-20251001')")
    conn.commit()
    return conn

conn = init_sticky_db()

def get_config(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM canary_config").fetchall()
    return {k: v for k, v in rows}

def assign_model_for_user(conn, user_id: str) -> str:
    """Assign user to a model cohort deterministically + persistently."""
    # Check existing assignment
    row = conn.execute("SELECT assigned_model FROM user_assignments WHERE user_id=?", (user_id,)).fetchone()
    if row:
        conn.execute("UPDATE user_assignments SET request_count=request_count+1 WHERE user_id=?", (user_id,))
        conn.commit()
        return row[0]

    # New user: assign based on hash (deterministic bucketing)
    cfg = get_config(conn)
    canary_pct = float(cfg.get("canary_pct", "0.1"))
    user_bucket = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % 100
    model = cfg["canary_model"] if user_bucket < canary_pct * 100 else cfg["stable_model"]

    conn.execute(
        "INSERT INTO user_assignments (user_id, assigned_model) VALUES (?, ?)",
        (user_id, model)
    )
    conn.commit()
    print(f"[sticky] User {user_id} → {model} (bucket={user_bucket})")
    return model

def update_canary_percentage(conn, new_pct: float):
    """Gradually increase canary traffic (or rollback to 0)."""
    conn.execute("UPDATE canary_config SET value=? WHERE key='canary_pct'", (str(new_pct),))
    if new_pct == 0.0:
        # Rollback: reassign canary users back to stable
        cfg = get_config(conn)
        conn.execute(
            "UPDATE user_assignments SET assigned_model=? WHERE assigned_model=?",
            (cfg["stable_model"], cfg["canary_model"])
        )
        print(f"[sticky] ROLLBACK: All users moved back to stable model")
    conn.commit()
    print(f"[sticky] Canary traffic updated to {new_pct*100:.0f}%")

def run_sticky_canary_agent(user_id: str, query: str) -> str:
    model = assign_model_for_user(conn, user_id)
    response = client.messages.create(
        model=model,
        max_tokens=128,
        messages=[{"role": "user", "content": query}]
    )
    return response.content[0].text

# Simulate 10 users across multiple requests
USERS = [f"user-{i:03d}" for i in range(10)]
for user in USERS:
    run_sticky_canary_agent(user, "What is machine learning?")

# Same users get same model on repeat requests
print("\n--- Second round (should use same models) ---")
for user in USERS[:3]:
    row = conn.execute("SELECT assigned_model, request_count FROM user_assignments WHERE user_id=?", (user,)).fetchone()
    print(f"{user}: {row[0]} ({row[1]} requests)")

# Gradually increase canary
update_canary_percentage(conn, 0.25)  # 25%
update_canary_percentage(conn, 0.50)  # 50%
# On regression: rollback
# update_canary_percentage(conn, 0.0)

# Expected Token Savings: ~0% (routing logic only; no token impact)
# Environment: Multi-tenant SaaS; customer-specific experiences require consistency across sessions
```

---

## Option 3: Quality-Gated Promotion — Auto-Promote Based on Eval Scores

Run automated quality evaluations on canary responses; auto-promote the canary model when it passes.

```python
import anthropic
import json
import random
import sqlite3
import time
from pathlib import Path

client = anthropic.Anthropic()
EVAL_DB = Path("/tmp/canary_eval.db")

def init_eval_db() -> sqlite3.Connection:
    conn = sqlite3.connect(EVAL_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            query TEXT,
            response TEXT,
            eval_score REAL,
            eval_dimension TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

EVAL_SYSTEM = """You are an AI response quality evaluator. Rate the response on a 0-10 scale for:
- accuracy: factual correctness
- helpfulness: how well it answers the question
- conciseness: appropriate length without padding

Reply with JSON: {"accuracy": 0-10, "helpfulness": 0-10, "conciseness": 0-10, "overall": 0-10}"""

def evaluate_response(query: str, response: str) -> dict:
    """Use an evaluator model to score a response."""
    eval_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=EVAL_SYSTEM,
        messages=[{"role": "user", "content": f"Query: {query}\n\nResponse: {response[:300]}"}]
    )
    try:
        return json.loads(eval_response.content[0].text)
    except json.JSONDecodeError:
        return {"overall": 7.0}

def get_model_eval_stats(conn, model: str, min_samples: int = 10) -> dict | None:
    rows = conn.execute("""
        SELECT COUNT(*), AVG(eval_score) FROM eval_results WHERE model=?
    """, (model,)).fetchone()
    total, avg_score = rows
    if (total or 0) < min_samples:
        return None
    return {"total": total, "avg_score": avg_score or 0}

class QualityGatedCanary:
    def __init__(self, stable: str, canary: str, canary_pct: float = 0.2):
        self.stable_model = stable
        self.canary_model = canary
        self.canary_pct = canary_pct
        self.current_model = stable  # Active production model
        self.conn = init_eval_db()
        self.promotion_threshold = 7.5  # Avg score needed to promote
        self.min_samples = 10

    def get_model(self) -> str:
        if random.random() < self.canary_pct:
            return self.canary_model
        return self.current_model

    def call_and_evaluate(self, query: str) -> dict:
        model = self.get_model()
        response = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": query}]
        )
        answer = response.content[0].text

        # Evaluate canary responses automatically
        if model == self.canary_model:
            scores = evaluate_response(query, answer)
            overall = scores.get("overall", 0)
            self.conn.execute(
                "INSERT INTO eval_results (model, query, response, eval_score) VALUES (?, ?, ?, ?)",
                (model, query[:100], answer[:200], overall)
            )
            self.conn.commit()

            # Check for auto-promotion
            stats = get_model_eval_stats(self.conn, self.canary_model, self.min_samples)
            if stats and stats["avg_score"] >= self.promotion_threshold:
                if self.current_model != self.canary_model:
                    print(f"[canary] AUTO-PROMOTING {self.canary_model} "
                          f"(avg_score={stats['avg_score']:.1f}, n={stats['total']})")
                    self.current_model = self.canary_model
                    self.canary_pct = 0.0  # Stop splitting; new model is now stable

        return {"model": model, "answer": answer}

    def status(self) -> dict:
        stable_stats = get_model_eval_stats(self.conn, self.stable_model, 0)
        canary_stats = get_model_eval_stats(self.conn, self.canary_model, 0)
        return {
            "current_production_model": self.current_model,
            "canary_traffic_pct": self.canary_pct,
            "canary_eval": canary_stats
        }

canary = QualityGatedCanary(
    stable="claude-haiku-4-5-20251001",
    canary="claude-haiku-4-5-20251001",  # Same model for demo; replace with new version
    canary_pct=0.3
)

TEST_QUERIES = [
    "Explain gradient descent in machine learning",
    "What are the SOLID principles in software design?",
    "How does HTTPS encryption work?",
    "What is the difference between SQL and NoSQL?",
    "Explain the CAP theorem",
]

for q in TEST_QUERIES * 3:  # Run multiple rounds to accumulate eval data
    result = canary.call_and_evaluate(q)

print(f"\n[canary] Status: {canary.status()}")

# Expected Token Savings: ~-20% (eval calls add cost; prevents bad model from reaching all users)
# Environment: Quality-sensitive agents; production systems with SLA requirements
```

---

## Option 4: Shadow Mode — Run New Model Silently and Compare

Run both models on every request; serve the stable model's response but log the comparison.

```python
import anthropic
import asyncio
import json
import sqlite3
from pathlib import Path
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()
SHADOW_DB = Path("/tmp/shadow_mode.db")

def init_shadow_db() -> sqlite3.Connection:
    conn = sqlite3.connect(SHADOW_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            stable_response TEXT,
            shadow_response TEXT,
            stable_tokens INTEGER,
            shadow_tokens INTEGER,
            stable_latency_ms INTEGER,
            shadow_latency_ms INTEGER,
            length_diff_pct REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

conn = init_shadow_db()

async def call_model_async(model: str, query: str) -> tuple[str, int, int]:
    """Returns (response_text, tokens, latency_ms)."""
    import time
    start = time.time()
    response = await client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": query}]
    )
    latency = round((time.time() - start) * 1000)
    return response.content[0].text, response.usage.output_tokens, latency

async def shadow_mode_call(
    query: str,
    stable_model: str = "claude-haiku-4-5-20251001",
    shadow_model: str = "claude-haiku-4-5-20251001"  # Replace with new model
) -> str:
    """Run both models concurrently; return stable response; log comparison."""
    # Run both models in parallel
    stable_task = asyncio.create_task(call_model_async(stable_model, query))
    shadow_task = asyncio.create_task(call_model_async(shadow_model, query))

    stable_result, shadow_result = await asyncio.gather(
        stable_task, shadow_task, return_exceptions=True
    )

    # Unpack stable result (always returned to user)
    if isinstance(stable_result, Exception):
        print(f"[shadow] Stable model failed: {stable_result}")
        return f"Error: {stable_result}"

    stable_text, stable_tokens, stable_latency = stable_result

    # Log shadow comparison (async, non-blocking to user)
    if not isinstance(shadow_result, Exception):
        shadow_text, shadow_tokens, shadow_latency = shadow_result
        length_diff_pct = (len(shadow_text) - len(stable_text)) / max(len(stable_text), 1) * 100
        conn.execute("""
            INSERT INTO shadow_comparisons
            (query, stable_response, shadow_response, stable_tokens, shadow_tokens,
             stable_latency_ms, shadow_latency_ms, length_diff_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (query[:100], stable_text[:300], shadow_text[:300],
              stable_tokens, shadow_tokens, stable_latency, shadow_latency, length_diff_pct))
        conn.commit()
        print(f"[shadow] stable={stable_latency}ms/{stable_tokens}tok | "
              f"shadow={shadow_latency}ms/{shadow_tokens}tok | "
              f"length_diff={length_diff_pct:+.1f}%")
    else:
        print(f"[shadow] Shadow model failed (stable still served): {shadow_result}")

    return stable_text  # Always return stable response to user

def get_shadow_report(conn) -> dict:
    rows = conn.execute("""
        SELECT
            COUNT(*),
            AVG(stable_tokens), AVG(shadow_tokens),
            AVG(stable_latency_ms), AVG(shadow_latency_ms),
            AVG(length_diff_pct)
        FROM shadow_comparisons
    """).fetchone()
    if not rows[0]:
        return {"samples": 0}
    return {
        "samples": rows[0],
        "avg_stable_tokens": round(rows[1] or 0),
        "avg_shadow_tokens": round(rows[2] or 0),
        "avg_stable_latency_ms": round(rows[3] or 0),
        "avg_shadow_latency_ms": round(rows[4] or 0),
        "avg_length_diff_pct": round(rows[5] or 0, 1)
    }

async def main():
    queries = [
        "What is dependency injection?",
        "Explain the observer design pattern",
        "What are Python context managers used for?",
    ]
    for q in queries:
        result = await shadow_mode_call(q)
        print(f"User sees: {result[:80]}...\n")

    print("\n[shadow] Report:", json.dumps(get_shadow_report(conn), indent=2))

asyncio.run(main())

# Expected Token Savings: ~-100% (both models run; shadow doubles LLM cost but provides zero-risk comparison)
# Environment: Pre-promotion validation; understanding behavioral differences before committing to a new model
```

---

## Option 5: Feature-Flag Canary — Toggle Model Per Request Feature

Use feature flags to control model selection at the request level; enable via API header or user segment.

```python
import anthropic
import json
import os
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class FeatureFlag:
    name: str
    enabled: bool = False
    percentage: float = 0.0  # 0.0-1.0
    enabled_user_ids: set = None
    enabled_segments: set = None

    def __post_init__(self):
        if self.enabled_user_ids is None:
            self.enabled_user_ids = set()
        if self.enabled_segments is None:
            self.enabled_segments = set()

class FeatureFlagRegistry:
    def __init__(self):
        self._flags: dict[str, FeatureFlag] = {}

    def register(self, flag: FeatureFlag):
        self._flags[flag.name] = flag

    def is_enabled(self, flag_name: str, user_id: str = "", segment: str = "") -> bool:
        flag = self._flags.get(flag_name)
        if not flag:
            return False
        if not flag.enabled:
            return False
        if user_id in flag.enabled_user_ids:
            return True
        if segment and segment in flag.enabled_segments:
            return True
        if flag.percentage > 0:
            import hashlib
            bucket = int(hashlib.md5(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
            return bucket < flag.percentage * 100
        return False

    def update(self, flag_name: str, **kwargs):
        flag = self._flags.get(flag_name)
        if flag:
            for k, v in kwargs.items():
                setattr(flag, k, v)
            print(f"[feature-flag] Updated {flag_name}: {kwargs}")

flags = FeatureFlagRegistry()
flags.register(FeatureFlag(
    name="use_new_claude_model",
    enabled=True,
    percentage=0.15,  # 15% of traffic
    enabled_user_ids={"beta-tester-001", "beta-tester-002"},
    enabled_segments={"internal_users"}
))

MODEL_MATRIX = {
    True: "claude-sonnet-4-6",          # New model (canary)
    False: "claude-haiku-4-5-20251001"  # Stable model
}

def run_feature_flagged_agent(
    query: str,
    user_id: str = "anon",
    user_segment: str = ""
) -> dict:
    use_new_model = flags.is_enabled("use_new_claude_model", user_id, user_segment)
    model = MODEL_MATRIX[use_new_model]
    print(f"[feature-flag] user={user_id} segment={user_segment} → "
          f"{'NEW' if use_new_model else 'stable'} model: {model}")

    response = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": query}]
    )

    return {
        "answer": response.content[0].text,
        "model": model,
        "is_canary": use_new_model,
        "user_id": user_id
    }

# Test different users
test_cases = [
    ("anon-123", ""),              # Random user, may or may not get canary
    ("beta-tester-001", ""),       # Always gets canary (explicit opt-in)
    ("regular-user", ""),          # Regular user
    ("employee-99", "internal_users"),  # Internal user segment → always canary
]

for uid, segment in test_cases:
    result = run_feature_flagged_agent("What is idempotency?", uid, segment)
    tag = "[CANARY]" if result["is_canary"] else "[stable]"
    print(f"  {tag} {result['answer'][:60]}...\n")

# Gradual rollout: increase to 30%
flags.update("use_new_claude_model", percentage=0.30)

# Rollback: disable entirely
# flags.update("use_new_claude_model", enabled=False)

# Expected Token Savings: ~0% (routing only; no token impact)
# Environment: SaaS platforms with existing feature flag infrastructure (LaunchDarkly, Unleash, custom)
```

---

## Option 6: Automated Rollback on Quality Degradation

Continuously monitor response quality metrics and automatically revert to the stable model on degradation.

```python
import anthropic
import json
import re
import sqlite3
import time
import threading
from pathlib import Path

client = anthropic.Anthropic()
MONITOR_DB = Path("/tmp/quality_monitor.db")

def init_monitor_db() -> sqlite3.Connection:
    conn = sqlite3.connect(MONITOR_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            threshold REAL,
            violated INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

class QualityMonitor:
    def __init__(self, conn, window_minutes: int = 10):
        self.conn = conn
        self.window = window_minutes
        self._lock = threading.Lock()

    def record(self, model: str, metric: str, value: float, threshold: float = None):
        violated = int(threshold is not None and value < threshold)
        with self._lock:
            self.conn.execute(
                "INSERT INTO quality_events (model, metric, value, threshold, violated) VALUES (?, ?, ?, ?, ?)",
                (model, metric, value, threshold, violated)
            )
            self.conn.commit()

    def violation_rate(self, model: str, metric: str = None) -> float:
        """Return fraction of recent events that violated their threshold."""
        q = f"SELECT COUNT(*), SUM(violated) FROM quality_events WHERE model=? AND created_at >= datetime('now', '-{self.window} minutes')"
        params = [model]
        if metric:
            q += " AND metric=?"
            params.append(metric)
        row = self.conn.execute(q, params).fetchone()
        total, violations = row
        if not total:
            return 0.0
        return (violations or 0) / total

class AutoRollbackCanary:
    def __init__(
        self,
        stable_model: str,
        canary_model: str,
        canary_pct: float = 0.2,
        violation_threshold: float = 0.15  # 15% violation rate triggers rollback
    ):
        self.stable_model = stable_model
        self.canary_model = canary_model
        self.canary_pct = canary_pct
        self.violation_threshold = violation_threshold
        self._use_canary = True
        self.monitor = QualityMonitor(init_monitor_db())
        self._rollback_lock = threading.Lock()

    def select_model(self) -> str:
        if not self._use_canary:
            return self.stable_model
        import random
        return self.canary_model if random.random() < self.canary_pct else self.stable_model

    def check_and_maybe_rollback(self, model: str) -> bool:
        """Returns True if rollback was triggered."""
        if model != self.canary_model:
            return False
        vrate = self.monitor.violation_rate(model)
        if vrate >= self.violation_threshold:
            with self._rollback_lock:
                if self._use_canary:
                    print(f"\n[auto-rollback] TRIGGERED: violation_rate={vrate:.1%} >= {self.violation_threshold:.1%}")
                    print(f"[auto-rollback] Switching ALL traffic to {self.stable_model}")
                    self._use_canary = False
                    return True
        return False

    def call(self, query: str) -> dict:
        model = self.select_model()
        start = time.time()

        try:
            response = client.messages.create(
                model=model,
                max_tokens=200,
                messages=[{"role": "user", "content": query}]
            )
            answer = response.content[0].text
            latency = (time.time() - start) * 1000
            tokens = response.usage.output_tokens

            # Record quality metrics
            self.monitor.record(model, "latency_ms", latency, threshold=5000)
            self.monitor.record(model, "output_tokens", tokens, threshold=10)
            self.monitor.record(model, "success", 1.0)

            # Check for rollback
            self.check_and_maybe_rollback(model)

            return {"model": model, "answer": answer, "tokens": tokens}

        except Exception as e:
            self.monitor.record(model, "success", 0.0, threshold=1.0)
            self.check_and_maybe_rollback(model)
            return {"model": model, "error": str(e)}

    @property
    def status(self) -> str:
        return "canary_active" if self._use_canary else "ROLLED_BACK"

canary = AutoRollbackCanary(
    stable_model="claude-haiku-4-5-20251001",
    canary_model="claude-haiku-4-5-20251001",
    canary_pct=0.3,
    violation_threshold=0.15
)

for i in range(15):
    result = canary.call(f"Question about topic {i % 4}")
    print(f"[{canary.status}] model={result['model'][-20:]} | {str(result.get('answer', result.get('error', '')))[:50]}...")

print(f"\nFinal status: {canary.status}")

# Expected Token Savings: ~0% (monitoring logic; no token impact)
# Environment: Mission-critical agents; 24/7 production systems requiring zero-downtime model updates
```

---

## Comparison

| Option | Traffic Split | Per-User Consistency | Auto-Rollback | Comparison Data | Best For |
|--------|--------------|---------------------|---------------|-----------------|----------|
| 1. Weighted Random | Random % | No | On error rate | Basic metrics | Simple canary rollout |
| 2. Sticky Canary | Deterministic hash | Yes | Manual | None | UX consistency across sessions |
| 3. Quality-Gated | Random % | No | On eval score | Eval scores | Quality-driven auto-promotion |
| 4. Shadow Mode | 100% both | N/A | No | Full comparison | Pre-promotion zero-risk analysis |
| 5. Feature Flags | Flag-based | Yes | Manual | None | Existing feature flag infrastructure |
| 6. Auto-Rollback Monitor | Random % | No | On violations | Violation rate | 24/7 production safety net |
