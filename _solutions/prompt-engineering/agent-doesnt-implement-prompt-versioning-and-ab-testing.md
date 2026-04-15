---
layout: solution
title: "Agent Doesn't Implement Prompt Versioning and A/B Testing"
category: prompt-engineering
description: "Prompt changes are shipped without tracking which version is deployed or how it performs. Prompt versioning and A/B testing enable safe rollouts, rollbacks, and data-driven prompt optimization backed by quality metrics."
tags: [prompt-engineering, versioning, ab-testing, quality, experimentation, rollout]
---

# Agent Doesn't Implement Prompt Versioning and A/B Testing

## Problem

Teams iterate on system prompts by editing a string in source code, with no history of what changed or why. There is no way to roll back a bad prompt, no data on whether a new prompt is actually better, and no controlled way to gradually release prompt changes. Quality regressions are discovered by user complaints, not metrics.

## Why This Happens

Prompts are treated as code comments — easy to change, easy to lose. Unlike model weights, prompts feel lightweight and safe to edit freely. But a bad system prompt can silently degrade every interaction. Without a versioning and testing framework, every prompt change is a blind deployment.

## Solutions

### Option 1: File-Based Prompt Registry — Version prompts as files with metadata

```python
import anthropic
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROMPT_DIR = Path("/tmp/prompts")
PROMPT_DIR.mkdir(exist_ok=True)

@dataclass
class PromptVersion:
    name: str
    version: str
    content: str
    author: str = "unknown"
    description: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    tags: list[str] = field(default_factory=list)


class PromptRegistry:
    def __init__(self, registry_dir: Path = PROMPT_DIR):
        self.dir = registry_dir

    def save(self, prompt: PromptVersion) -> Path:
        path = self.dir / f"{prompt.name}_v{prompt.version}.json"
        path.write_text(json.dumps({
            "name": prompt.name,
            "version": prompt.version,
            "content": prompt.content,
            "author": prompt.author,
            "description": prompt.description,
            "created_at": prompt.created_at,
            "tags": prompt.tags,
        }, indent=2))
        # Update "latest" pointer
        latest = self.dir / f"{prompt.name}_latest.json"
        latest.write_text(json.dumps({"points_to": str(path)}))
        return path

    def load(self, name: str, version: str | None = None) -> PromptVersion:
        if version:
            path = self.dir / f"{name}_v{version}.json"
        else:
            latest = self.dir / f"{name}_latest.json"
            if not latest.exists():
                raise FileNotFoundError(f"No prompt found for '{name}'")
            pointed = json.loads(latest.read_text())["points_to"]
            path = Path(pointed)

        data = json.loads(path.read_text())
        return PromptVersion(**data)

    def list_versions(self, name: str) -> list[str]:
        return sorted([
            f.stem.replace(f"{name}_v", "")
            for f in self.dir.glob(f"{name}_v*.json")
        ])

    def rollback(self, name: str, to_version: str) -> PromptVersion:
        prompt = self.load(name, version=to_version)
        # Re-save as latest
        self.save(prompt)
        print(f"[ROLLBACK] '{name}' → v{to_version}")
        return prompt


class VersionedAgent:
    def __init__(self, prompt_name: str, version: str | None = None):
        self.client = anthropic.Anthropic()
        self.registry = PromptRegistry()
        self.prompt = self.registry.load(prompt_name, version=version)
        print(f"[PROMPT] Loaded '{self.prompt.name}' v{self.prompt.version}: {self.prompt.description}")

    def chat(self, user_message: str) -> str:
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=self.prompt.content,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text


# Setup: register prompt versions
registry = PromptRegistry()
registry.save(PromptVersion(
    name="customer_support",
    version="1.0",
    content="You are a helpful customer support agent. Be polite and concise.",
    author="alice",
    description="Initial prompt — baseline",
))
registry.save(PromptVersion(
    name="customer_support",
    version="1.1",
    content="You are an expert customer support agent. Always acknowledge the customer's frustration before solving their problem. Keep responses under 3 sentences.",
    author="bob",
    description="Added empathy instruction and length constraint",
    tags=["empathy", "conciseness"],
))

# Use latest version
agent = VersionedAgent("customer_support")
print(agent.chat("My order hasn't arrived after 2 weeks!"))

# Rollback example
registry.rollback("customer_support", to_version="1.0")

# Expected Token Savings: Enables rollback when new prompt causes verbosity regression (saves output tokens)
# Environment: Production agents, customer-facing chatbots, any deployment requiring prompt governance
```

### Option 2: SQLite Prompt Store with Metrics — Track performance per version in DB

```python
import anthropic
import sqlite3
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/tmp/prompt_versions.db")

@dataclass
class PromptMetrics:
    version_id: int
    response_length_avg: float
    user_satisfaction_avg: float  # 0-1 from thumbs up/down
    latency_ms_avg: float
    call_count: int


class PromptVersionDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.db = db_path
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    content TEXT NOT NULL,
                    description TEXT,
                    author TEXT,
                    created_at TEXT,
                    is_active INTEGER DEFAULT 0,
                    UNIQUE(name, version)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    latency_ms REAL,
                    user_rating REAL,
                    timestamp TEXT,
                    FOREIGN KEY(version_id) REFERENCES prompt_versions(id)
                )
            """)

    def save_version(self, name: str, version: str, content: str,
                     description: str = "", author: str = "") -> int:
        with sqlite3.connect(self.db) as conn:
            cursor = conn.execute(
                "INSERT OR REPLACE INTO prompt_versions(name,version,content,description,author,created_at) VALUES(?,?,?,?,?,?)",
                (name, version, content, description, author, datetime.utcnow().isoformat())
            )
            return cursor.lastrowid

    def set_active(self, name: str, version: str) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE prompt_versions SET is_active=0 WHERE name=?", (name,))
            conn.execute("UPDATE prompt_versions SET is_active=1 WHERE name=? AND version=?", (name, version))

    def get_active(self, name: str) -> tuple[int, str] | None:
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT id, content FROM prompt_versions WHERE name=? AND is_active=1", (name,)
            ).fetchone()
        return row  # (id, content) or None

    def record_call(self, version_id: int, input_tokens: int, output_tokens: int,
                    latency_ms: float, user_rating: float | None = None) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO prompt_calls(version_id,input_tokens,output_tokens,latency_ms,user_rating,timestamp) VALUES(?,?,?,?,?,?)",
                (version_id, input_tokens, output_tokens, latency_ms, user_rating, datetime.utcnow().isoformat())
            )

    def version_metrics(self, name: str) -> list[dict]:
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("""
                SELECT pv.version, pv.description,
                       COUNT(pc.id) as calls,
                       AVG(pc.output_tokens) as avg_output_tokens,
                       AVG(pc.latency_ms) as avg_latency_ms,
                       AVG(pc.user_rating) as avg_rating
                FROM prompt_versions pv
                LEFT JOIN prompt_calls pc ON pc.version_id = pv.id
                WHERE pv.name = ?
                GROUP BY pv.id
                ORDER BY pv.created_at
            """, (name,)).fetchall()
        return [
            {
                "version": r[0], "description": r[1], "calls": r[2],
                "avg_output_tokens": round(r[3] or 0, 1),
                "avg_latency_ms": round(r[4] or 0, 1),
                "avg_rating": round(r[5] or 0, 2),
            }
            for r in rows
        ]


class MetricTrackedAgent:
    def __init__(self, prompt_name: str):
        import time
        self.time = time
        self.client = anthropic.Anthropic()
        self.db = PromptVersionDB()
        self.prompt_name = prompt_name

        result = self.db.get_active(prompt_name)
        if not result:
            raise ValueError(f"No active prompt version for '{prompt_name}'")
        self.version_id, self.system_prompt = result

    def chat(self, user_message: str, user_rating: float | None = None) -> str:
        start = self.time.time()
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        latency = (self.time.time() - start) * 1000
        self.db.record_call(
            version_id=self.version_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency,
            user_rating=user_rating,
        )
        return response.content[0].text


# Setup
db = PromptVersionDB()
db.save_version("assistant", "2.0", "You are a helpful assistant.", description="Baseline")
db.save_version("assistant", "2.1", "You are a concise, expert assistant. Answer in 2 sentences max.", description="Conciseness experiment")
db.set_active("assistant", "2.1")

agent = MetricTrackedAgent("assistant")
agent.chat("What is a REST API?", user_rating=0.9)
agent.chat("Explain Docker.", user_rating=0.7)

print(json.dumps(db.version_metrics("assistant"), indent=2))

# Expected Token Savings: Data shows which versions are verbose; v2.1 constraint saves ~30% output tokens
# Environment: Any production chatbot; enables data-driven prompt optimization over time
```

### Option 3: Traffic-Split A/B Test — Route N% of traffic to variant B; compare metrics

```python
import anthropic
import hashlib
import json
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class ABVariant:
    name: str
    system_prompt: str
    traffic_pct: float  # 0.0-1.0


class ABTestRouter:
    def __init__(self, variants: list[ABVariant]):
        assert abs(sum(v.traffic_pct for v in variants) - 1.0) < 0.01, "Traffic % must sum to 1.0"
        self.variants = variants
        self._metrics: dict[str, list[float]] = defaultdict(list)

    def route(self, user_id: str) -> ABVariant:
        """Deterministic routing: same user always gets same variant."""
        bucket = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % 100
        cumulative = 0.0
        for variant in self.variants:
            cumulative += variant.traffic_pct * 100
            if bucket < cumulative:
                return variant
        return self.variants[-1]

    def record_metric(self, variant_name: str, metric: float) -> None:
        self._metrics[variant_name].append(metric)

    def report(self) -> dict:
        result = {}
        for variant_name, scores in self._metrics.items():
            result[variant_name] = {
                "n": len(scores),
                "mean": round(sum(scores) / len(scores), 3) if scores else 0,
                "min": round(min(scores), 3) if scores else 0,
                "max": round(max(scores), 3) if scores else 0,
            }
        return result


class ABTestingAgent:
    def __init__(self, router: ABTestRouter):
        self.client = anthropic.Anthropic()
        self.router = router

    def chat(self, user_id: str, user_message: str) -> tuple[str, str]:
        variant = self.router.route(user_id)
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=variant.system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        text = response.content[0].text
        # Track output length as a quality proxy (shorter = more concise)
        self.router.record_metric(variant.name, len(text.split()))
        return text, variant.name


# Setup A/B test: verbose vs concise system prompt
router = ABTestRouter([
    ABVariant(
        name="control",
        system_prompt="You are a helpful assistant. Provide thorough, detailed answers.",
        traffic_pct=0.5,
    ),
    ABVariant(
        name="concise",
        system_prompt="You are a helpful assistant. Answer in 3 sentences or fewer. Be direct.",
        traffic_pct=0.5,
    ),
])

agent = ABTestingAgent(router)

questions = [
    "What is machine learning?",
    "How does the internet work?",
    "What is a database?",
    "Explain APIs.",
    "What is cloud computing?",
]

for i, question in enumerate(questions * 4):  # Simulate 20 users
    user_id = f"user-{i}"
    reply, variant = agent.chat(user_id, question)
    print(f"[{variant}] User {user_id}: {len(reply.split())} words")

print("\n=== A/B Test Results ===")
print(json.dumps(router.report(), indent=2))

# Expected Token Savings: Concise variant typically 40-60% fewer output tokens; A/B proves this empirically
# Environment: Prompt optimization experiments, cost reduction initiatives, quality improvement projects
```

### Option 4: Canary Deployment — Roll out new prompt to 5% of users; auto-promote on success

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum

class RolloutState(Enum):
    CANARY = "canary"         # 5% traffic
    PARTIAL = "partial"       # 25% traffic
    MAJORITY = "majority"     # 75% traffic
    FULL = "full"             # 100% traffic
    ROLLED_BACK = "rolled_back"

ROLLOUT_TRAFFIC = {
    RolloutState.CANARY: 0.05,
    RolloutState.PARTIAL: 0.25,
    RolloutState.MAJORITY: 0.75,
    RolloutState.FULL: 1.0,
    RolloutState.ROLLED_BACK: 0.0,
}

@dataclass
class CanaryRollout:
    stable_prompt: str
    canary_prompt: str
    state: RolloutState = RolloutState.CANARY
    canary_calls: int = 0
    canary_errors: int = 0
    promote_after: int = 50    # Calls before auto-promoting
    error_threshold: float = 0.1  # Auto-rollback above 10% error rate

    @property
    def canary_traffic(self) -> float:
        return ROLLOUT_TRAFFIC[self.state]

    def is_canary(self, user_id: str) -> bool:
        if self.state == RolloutState.ROLLED_BACK:
            return False
        if self.state == RolloutState.FULL:
            return True
        bucket = int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % 100
        return bucket < int(self.canary_traffic * 100)

    def record(self, used_canary: bool, error: bool) -> str | None:
        """Returns action taken: 'promoted', 'rolled_back', or None."""
        if not used_canary:
            return None

        self.canary_calls += 1
        if error:
            self.canary_errors += 1

        error_rate = self.canary_errors / self.canary_calls

        # Auto-rollback on high error rate
        if error_rate > self.error_threshold and self.canary_calls >= 10:
            self.state = RolloutState.ROLLED_BACK
            print(f"[CANARY] AUTO-ROLLBACK: error rate {error_rate:.0%} > {self.error_threshold:.0%}")
            return "rolled_back"

        # Auto-promote on success
        if self.canary_calls >= self.promote_after and error_rate < self.error_threshold:
            transitions = {
                RolloutState.CANARY: RolloutState.PARTIAL,
                RolloutState.PARTIAL: RolloutState.MAJORITY,
                RolloutState.MAJORITY: RolloutState.FULL,
            }
            next_state = transitions.get(self.state)
            if next_state:
                self.state = next_state
                self.canary_calls = self.canary_errors = 0  # Reset window
                print(f"[CANARY] PROMOTED to {self.state.value} ({self.canary_traffic:.0%} traffic)")
                return "promoted"
        return None

    def active_prompt(self, user_id: str) -> tuple[str, str]:
        if self.is_canary(user_id):
            return self.canary_prompt, "canary"
        return self.stable_prompt, "stable"


class CanaryAgent:
    def __init__(self, rollout: CanaryRollout):
        self.client = anthropic.Anthropic()
        self.rollout = rollout

    def chat(self, user_id: str, message: str) -> tuple[str, str, str | None]:
        prompt, variant = self.rollout.active_prompt(user_id)
        error_occurred = False

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=prompt,
                messages=[{"role": "user", "content": message}]
            )
            text = response.content[0].text
        except Exception as e:
            text = f"Error: {e}"
            error_occurred = True

        action = self.rollout.record(variant == "canary", error_occurred)
        return text, variant, action


# Usage
rollout = CanaryRollout(
    stable_prompt="You are a helpful assistant.",
    canary_prompt="You are a concise assistant. Reply in 2 sentences max.",
    promote_after=5,  # Low for demo; use 50-100 in production
)

agent = CanaryAgent(rollout)

for i in range(20):
    user_id = f"user-{i:04d}"
    reply, variant, action = agent.chat(user_id, "What is Python?")
    print(f"[{variant:6s}] user-{i}: {reply[:60]}...")
    if action:
        print(f"  *** {action.upper()} ***")

print(f"\nFinal state: {rollout.state.value}")

# Expected Token Savings: Canary catches verbose regressions before full rollout; limits blast radius
# Environment: Production services, any change affecting prompt behavior for real users
```

### Option 5: Prompt Diff Viewer — Show what changed between versions before deploying

```python
import anthropic
import difflib
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class PromptDiff:
    name: str
    from_version: str
    to_version: str
    from_content: str
    to_content: str

    def unified_diff(self) -> str:
        from_lines = self.from_content.splitlines(keepends=True)
        to_lines = self.to_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            from_lines, to_lines,
            fromfile=f"{self.name} v{self.from_version}",
            tofile=f"{self.name} v{self.to_version}",
        )
        return "".join(diff)

    def change_summary(self) -> dict:
        from_words = set(self.from_content.lower().split())
        to_words = set(self.to_content.lower().split())
        added = to_words - from_words
        removed = from_words - to_words
        return {
            "added_words": sorted(added)[:10],
            "removed_words": sorted(removed)[:10],
            "length_change": len(self.to_content) - len(self.from_content),
            "similarity": round(difflib.SequenceMatcher(None, self.from_content, self.to_content).ratio(), 2),
        }

    def impact_warning(self) -> list[str]:
        warnings = []
        summary = self.change_summary()
        if summary["similarity"] < 0.5:
            warnings.append("HIGH IMPACT: >50% of prompt content changed")
        if "not" in summary["added_words"] or "never" in summary["added_words"]:
            warnings.append("Negative constraints added — test for over-refusal")
        if summary["length_change"] > 200:
            warnings.append(f"Prompt grew by {summary['length_change']} chars — may increase input token cost")
        if summary["length_change"] < -100:
            warnings.append(f"Prompt shrank by {abs(summary['length_change'])} chars — verify no key instructions removed")
        return warnings


class VersionedPromptManager:
    def __init__(self, storage_dir: str = "/tmp/prompts_v2"):
        self.dir = Path(storage_dir)
        self.dir.mkdir(exist_ok=True)

    def save(self, name: str, version: str, content: str) -> None:
        path = self.dir / f"{name}_{version}.txt"
        path.write_text(content)

    def load(self, name: str, version: str) -> str:
        return (self.dir / f"{name}_{version}.txt").read_text()

    def diff(self, name: str, from_version: str, to_version: str) -> PromptDiff:
        return PromptDiff(
            name=name,
            from_version=from_version,
            to_version=to_version,
            from_content=self.load(name, from_version),
            to_content=self.load(name, to_version),
        )

    def review_before_deploy(self, name: str, from_version: str, to_version: str) -> bool:
        """Show diff and warnings; return True if safe to deploy."""
        diff = self.diff(name, from_version, to_version)

        print(f"\n{'='*50}")
        print(f"PROMPT DIFF: {name} v{from_version} → v{to_version}")
        print('='*50)
        print(diff.unified_diff() or "(no textual differences)")

        summary = diff.change_summary()
        print(f"\nSimilarity: {summary['similarity']:.0%}")
        print(f"Length change: {summary['length_change']:+d} chars")

        warnings = diff.impact_warning()
        if warnings:
            print("\n⚠ WARNINGS:")
            for w in warnings:
                print(f"  - {w}")
        else:
            print("\n✓ No significant impact warnings")

        return len(warnings) == 0 or summary["similarity"] >= 0.7


# Setup
mgr = VersionedPromptManager()
mgr.save("support", "3.0", "You are a helpful customer support agent. Be polite and thorough in your responses.")
mgr.save("support", "3.1", "You are a concise customer support agent. Never use more than 3 sentences. Be direct and solution-focused.")

safe = mgr.review_before_deploy("support", "3.0", "3.1")
print(f"\nDeploy recommendation: {'✓ SAFE' if safe else '⚠ REVIEW REQUIRED'}")

# Expected Token Savings: Catches accidental length increases before deployment; impact review prevents regressions
# Environment: Any team with >1 person editing prompts; CI/CD pipeline gate before prompt deploy
```

### Option 6: Automated Prompt Eval on Commit — Run eval suite before every prompt version goes live

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class EvalCase:
    id: str
    input: str
    expected_keywords: list[str]      # Must appear in output
    forbidden_keywords: list[str]     # Must NOT appear in output
    max_words: int | None = None      # Output word count limit

@dataclass
class EvalResult:
    case_id: str
    passed: bool
    failures: list[str]
    output: str
    word_count: int


class PromptEvalSuite:
    def __init__(self, cases: list[EvalCase]):
        self.client = anthropic.Anthropic()
        self.cases = cases

    def run(self, system_prompt: str, model: str = "claude-haiku-4-5-20251001") -> list[EvalResult]:
        results = []
        for case in self.cases:
            response = self.client.messages.create(
                model=model,
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": case.input}]
            )
            output = response.content[0].text
            words = output.lower().split()
            word_count = len(words)

            failures = []
            for kw in case.expected_keywords:
                if kw.lower() not in output.lower():
                    failures.append(f"Missing expected keyword: '{kw}'")
            for kw in case.forbidden_keywords:
                if kw.lower() in output.lower():
                    failures.append(f"Contains forbidden keyword: '{kw}'")
            if case.max_words and word_count > case.max_words:
                failures.append(f"Too long: {word_count} words > {case.max_words} limit")

            results.append(EvalResult(
                case_id=case.id,
                passed=len(failures) == 0,
                failures=failures,
                output=output[:200],
                word_count=word_count,
            ))

        return results

    def gate(self, system_prompt: str, min_pass_rate: float = 0.9) -> tuple[bool, dict]:
        results = self.run(system_prompt)
        passed = sum(1 for r in results if r.passed)
        pass_rate = passed / len(results)

        report = {
            "passed": passed,
            "total": len(results),
            "pass_rate": f"{pass_rate:.0%}",
            "gate_passed": pass_rate >= min_pass_rate,
            "failures": [
                {"case": r.case_id, "issues": r.failures}
                for r in results if not r.passed
            ],
        }
        return pass_rate >= min_pass_rate, report


# Define eval cases for a customer support prompt
eval_suite = PromptEvalSuite([
    EvalCase(
        id="greeting",
        input="Hello",
        expected_keywords=["help", "assist"],
        forbidden_keywords=["error", "sorry"],
    ),
    EvalCase(
        id="refund",
        input="I want a refund",
        expected_keywords=["refund"],
        forbidden_keywords=["cannot", "impossible", "no"],
        max_words=60,
    ),
    EvalCase(
        id="product_question",
        input="What features does your product have?",
        expected_keywords=["feature"],
        forbidden_keywords=[],
        max_words=80,
    ),
])

# Gate two prompt candidates
candidates = {
    "v4.0": "You are a helpful customer support agent. Answer all questions thoroughly and professionally.",
    "v4.1": "You are a brief, solution-focused support agent. Answer in 2 sentences. Never say 'cannot'.",
}

for version, prompt in candidates.items():
    gate_passed, report = eval_suite.gate(prompt, min_pass_rate=0.8)
    status = "✓ DEPLOY" if gate_passed else "✗ BLOCK"
    print(f"\n[{status}] {version}: {report['pass_rate']} pass rate")
    if report["failures"]:
        print(f"  Failures: {json.dumps(report['failures'], indent=2)}")

# Expected Token Savings: Blocks verbose prompt regressions at commit time; Haiku eval is low-cost gate
# Environment: CI/CD pipelines, automated prompt deployment, teams iterating on prompts frequently
```

## Comparison

| Option | Storage | Rollback | A/B Testing | Auto-Promote | Best For |
|--------|---------|----------|-------------|-------------|----------|
| File-Based Registry | Files | Manual | No | No | Small teams, simple versioning |
| SQLite with Metrics | SQLite | Manual | No | No | Data-driven iteration |
| Traffic-Split A/B | In-memory | Manual | Yes | No | Experiment-driven teams |
| Canary Deployment | In-memory | Auto | Partial | Yes | Safe production rollouts |
| Diff Viewer | Files | Manual | No | No | Review workflows, PR gates |
| Eval on Commit | In-memory | N/A | No | Conditional | CI/CD quality gates |
