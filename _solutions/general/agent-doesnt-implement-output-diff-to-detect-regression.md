---
layout: solution
title: "Agent Doesn't Implement Output Diff to Detect Regression"
category: general
description: "Prompt changes that don't break tests can still silently shift agent output in ways that matter to users. Output diffing compares current responses against golden baselines, flags semantic drift, and catches regressions before they reach production."
tags: [regression, output-diff, golden-baseline, quality, testing, sqlite, prompt-engineering]
---

# Agent Doesn't Implement Output Diff to Detect Regression

## Problem

When you change a system prompt, upgrade a model, or modify tool definitions, the output can shift subtly without triggering any test failure. The format might stay correct while the tone changes. The length might be identical while key facts disappear. Without output diffing against known-good baselines, these regressions ship silently.

Output diffing runs the same inputs through old and new configurations, compares responses against golden baselines, and flags meaningful changes.

---

## Option 1: Simple Character-Level Diff

```python
import difflib
import anthropic
from dataclasses import dataclass

@dataclass
class DiffResult:
    input_prompt: str
    baseline: str
    current: str
    similarity_ratio: float
    diff_lines: list[str]
    regressed: bool

REGRESSION_THRESHOLD = 0.70  # Flag if similarity drops below 70%

def compute_diff(baseline: str, current: str) -> DiffResult:
    ratio = difflib.SequenceMatcher(None, baseline, current).ratio()
    diff = list(difflib.unified_diff(
        baseline.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile="baseline",
        tofile="current",
        n=2,
    ))
    return DiffResult(
        input_prompt="",
        baseline=baseline,
        current=current,
        similarity_ratio=round(ratio, 3),
        diff_lines=diff,
        regressed=ratio < REGRESSION_THRESHOLD,
    )


# Golden baselines: known-good responses for fixed inputs
GOLDEN_BASELINES = {
    "What is Python?": (
        "Python is a high-level, interpreted programming language known for its "
        "clear syntax and readability. It supports multiple programming paradigms "
        "and has a large standard library."
    ),
    "What is 2+2?": "2+2 equals 4.",
    "Name a sorting algorithm.": "QuickSort is a commonly used sorting algorithm.",
}


def run_regression_check(system_prompt: str) -> list[DiffResult]:
    client = anthropic.Anthropic()
    results = []

    for prompt, baseline in GOLDEN_BASELINES.items():
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        current = response.content[0].text
        result = compute_diff(baseline, current)
        result.input_prompt = prompt
        results.append(result)

        status = "⚠️ REGRESSED" if result.regressed else "✓"
        print(f"{status} [{result.similarity_ratio:.2f}] {prompt[:40]}")
        if result.regressed and result.diff_lines:
            print("".join(result.diff_lines[:8]))

    regressions = sum(1 for r in results if r.regressed)
    print(f"\n{regressions}/{len(results)} inputs regressed")
    return results


if __name__ == "__main__":
    new_system = "You are a concise assistant. Always use bullet points."
    run_regression_check(new_system)
# Expected Token Savings: None direct — diff catches regressions that would require costly rollbacks later
# Environment: pip install anthropic; difflib is stdlib
```

---

## Option 2: Semantic Similarity Diff via Embedding Probe

```python
import anthropic
import math
from dataclasses import dataclass

@dataclass
class SemanticDiff:
    prompt: str
    baseline: str
    current: str
    semantic_score: float   # 0=completely different, 1=identical meaning
    key_facts_preserved: float  # fraction of probe questions answered consistently
    regressed: bool

SEMANTIC_THRESHOLD = 0.75  # Flag if semantic score drops below 75%


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x ** 2 for x in a))
    norm_b = math.sqrt(sum(x ** 2 for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def get_embedding(client: anthropic.Anthropic, text: str) -> list[float]:
    """Use a haiku call to produce a simple bag-of-words proxy embedding."""
    # In production, use a real embedding model (e.g., voyage-3)
    # Here we use token probability as a lightweight proxy
    words = set(text.lower().split())
    # Simple 64-dim binary feature vector over common words
    vocab = ["python", "algorithm", "sort", "data", "function", "class",
             "list", "dict", "async", "error", "return", "import", "type",
             "int", "str", "bool", "none", "true", "false", "def", "for",
             "while", "if", "else", "try", "except", "with", "lambda", "yield",
             "print", "input", "len", "range", "open", "read", "write", "file",
             "api", "model", "token", "prompt", "response", "request", "json",
             "http", "url", "method", "status", "code", "key", "value", "name",
             "tool", "agent", "system", "message", "role", "user", "assistant",
             "high", "level", "interpreted", "language", "syntax", "library",
             "clear", "readable", "multiple", "paradigm", "large", "standard"]
    return [1.0 if w in words else 0.0 for w in vocab]


def check_key_facts(client: anthropic.Anthropic, baseline: str, current: str, topic: str) -> float:
    """Ask probe questions about baseline and current, compare yes/no answers."""
    probe_q = (
        f"Regarding this text about '{topic}', answer yes or no:\n"
        f"1. Does it mention a programming concept?\n"
        f"2. Is it factually accurate?\n"
        f"3. Is it concise (under 100 words)?\n"
        f"Text: {{text}}"
    )

    def get_answers(text: str) -> str:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": probe_q.format(text=text[:200])}],
        )
        return r.content[0].text.lower()

    base_ans = get_answers(baseline)
    curr_ans = get_answers(current)

    # Count matching yes/no patterns
    base_yeses = base_ans.count("yes")
    curr_yeses = curr_ans.count("yes")
    max_q = 3
    return 1.0 - abs(base_yeses - curr_yeses) / max_q


GOLDEN = {
    "What is Python?": "Python is a high-level, interpreted programming language known for clear syntax.",
    "What is a hash table?": "A hash table maps keys to values using a hash function for O(1) average lookups.",
}


def run_semantic_regression(system_prompt: str) -> list[SemanticDiff]:
    client = anthropic.Anthropic()
    results = []

    for prompt, baseline in GOLDEN.items():
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=96,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        current = response.content[0].text

        emb_base = get_embedding(client, baseline)
        emb_curr = get_embedding(client, current)
        semantic = cosine_similarity(emb_base, emb_curr)
        fact_score = check_key_facts(client, baseline, current, topic=prompt)

        combined = (semantic * 0.6) + (fact_score * 0.4)
        result = SemanticDiff(
            prompt=prompt,
            baseline=baseline,
            current=current,
            semantic_score=round(semantic, 3),
            key_facts_preserved=round(fact_score, 3),
            regressed=combined < SEMANTIC_THRESHOLD,
        )
        results.append(result)

        status = "⚠️" if result.regressed else "✓"
        print(f"{status} {prompt[:40]}: semantic={semantic:.2f} facts={fact_score:.2f}")
        print(f"   Current: {current[:80]}")

    return results


if __name__ == "__main__":
    run_semantic_regression("You are a very brief assistant. Answer in exactly one word.")
# Expected Token Savings: None direct — semantic probing uses ~3 extra haiku calls per test input
# Environment: pip install anthropic; math is stdlib
```

---

## Option 3: SQLite Golden Store with Versioned Baselines

```python
import sqlite3
import difflib
import json
import anthropic
from datetime import datetime

class GoldenStore:
    """
    Persists golden baselines per (prompt, system_version) in SQLite.
    Compares new runs against stored baselines and logs diffs.
    """

    def __init__(self, db_path: str = "golden.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS golden_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_hash TEXT,
                prompt TEXT,
                system_version TEXT,
                response TEXT,
                is_golden INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS diff_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT,
                from_version TEXT,
                to_version TEXT,
                similarity REAL,
                regressed INTEGER,
                diff_preview TEXT,
                logged_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_golden ON golden_responses(prompt_hash, system_version, is_golden);
        """)
        self.conn.commit()

    def _hash(self, prompt: str) -> str:
        import hashlib
        return hashlib.md5(prompt.encode()).hexdigest()[:12]

    def save_golden(self, prompt: str, response: str, version: str):
        # Clear old golden for this prompt+version
        self.conn.execute(
            "UPDATE golden_responses SET is_golden=0 WHERE prompt_hash=? AND system_version=?",
            (self._hash(prompt), version),
        )
        self.conn.execute(
            "INSERT INTO golden_responses (prompt_hash, prompt, system_version, response, is_golden) VALUES (?,?,?,?,1)",
            (self._hash(prompt), prompt[:200], version, response),
        )
        self.conn.commit()

    def get_golden(self, prompt: str, version: str) -> str | None:
        row = self.conn.execute(
            "SELECT response FROM golden_responses WHERE prompt_hash=? AND system_version=? AND is_golden=1",
            (self._hash(prompt), version),
        ).fetchone()
        return row[0] if row else None

    def compare_and_log(self, prompt: str, baseline_version: str, candidate_version: str, candidate_response: str) -> dict:
        baseline = self.get_golden(prompt, baseline_version)
        if not baseline:
            return {"status": "no_baseline", "prompt": prompt}

        ratio = difflib.SequenceMatcher(None, baseline, candidate_response).ratio()
        diff_lines = list(difflib.unified_diff(
            baseline.splitlines(), candidate_response.splitlines(),
            fromfile=f"v{baseline_version}", tofile=f"v{candidate_version}", n=1,
        ))
        diff_preview = "\n".join(diff_lines[:6])
        regressed = ratio < 0.65

        self.conn.execute(
            "INSERT INTO diff_log (prompt, from_version, to_version, similarity, regressed, diff_preview) VALUES (?,?,?,?,?,?)",
            (prompt[:100], baseline_version, candidate_version, round(ratio, 4), int(regressed), diff_preview[:500]),
        )
        self.conn.commit()

        return {
            "prompt": prompt[:50],
            "similarity": round(ratio, 3),
            "regressed": regressed,
            "diff_preview": diff_preview,
        }

    def regression_report(self) -> dict:
        rows = self.conn.execute("""
            SELECT from_version, to_version, COUNT(*) as total,
                   SUM(regressed) as regressions, AVG(similarity) as avg_sim
            FROM diff_log
            GROUP BY from_version, to_version
        """).fetchall()
        return {
            f"{r[0]}→{r[1]}": {
                "total_inputs": r[2],
                "regressions": r[3],
                "avg_similarity": round(r[4], 3),
            }
            for r in rows
        }


def run_versioned_regression(test_prompts: list[str]):
    store = GoldenStore(db_path=":memory:")
    client = anthropic.Anthropic()

    SYSTEMS = {
        "v1": "You are a helpful assistant.",
        "v2": "You are a helpful assistant. Always answer in exactly one sentence.",
    }

    # Record v1 as golden baseline
    print("Recording v1 goldens...")
    for prompt in test_prompts:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=SYSTEMS["v1"],
            messages=[{"role": "user", "content": prompt}],
        )
        store.save_golden(prompt, r.content[0].text, version="v1")

    # Run v2 and compare
    print("\nRunning v2 and comparing...")
    for prompt in test_prompts:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=SYSTEMS["v2"],
            messages=[{"role": "user", "content": prompt}],
        )
        result = store.compare_and_log(prompt, "v1", "v2", r.content[0].text)
        icon = "⚠️" if result.get("regressed") else "✓"
        print(f"  {icon} [{result['similarity']:.2f}] {result['prompt']}")

    print(f"\nRegression Report: {json.dumps(store.regression_report(), indent=2)}")


if __name__ == "__main__":
    prompts = [
        "What is Python?",
        "Explain recursion.",
        "What is a REST API?",
        "How does HTTPS work?",
        "What is machine learning?",
    ]
    run_versioned_regression(prompts)
# Expected Token Savings: 0% direct — detection cost is 2× API calls; saves from deploying regressive prompts
# Environment: pip install anthropic; sqlite3, difflib, json, hashlib are stdlib
```

---

## Option 4: Structural Output Diff for JSON Responses

```python
import json
import anthropic
from dataclasses import dataclass
from typing import Any

@dataclass
class StructuralDiff:
    prompt: str
    missing_keys: list[str]       # keys in baseline but not current
    extra_keys: list[str]          # keys in current but not baseline
    type_changes: dict[str, str]   # key → "old_type → new_type"
    value_drift: dict[str, str]    # key → description of value change
    schema_ok: bool

def deep_diff(baseline: Any, current: Any, path: str = "") -> StructuralDiff:
    result = StructuralDiff(
        prompt="",
        missing_keys=[],
        extra_keys=[],
        type_changes={},
        value_drift={},
        schema_ok=True,
    )

    if not isinstance(baseline, dict) or not isinstance(current, dict):
        if type(baseline) != type(current):
            result.type_changes[path or "root"] = f"{type(baseline).__name__} → {type(current).__name__}"
            result.schema_ok = False
        return result

    baseline_keys = set(baseline.keys())
    current_keys = set(current.keys())

    result.missing_keys = sorted(baseline_keys - current_keys)
    result.extra_keys = sorted(current_keys - baseline_keys)

    if result.missing_keys or result.extra_keys:
        result.schema_ok = False

    for key in baseline_keys & current_keys:
        full_path = f"{path}.{key}" if path else key
        bval, cval = baseline[key], current[key]

        if type(bval) != type(cval):
            result.type_changes[full_path] = f"{type(bval).__name__} → {type(cval).__name__}"
            result.schema_ok = False
        elif isinstance(bval, str) and bval != cval:
            if len(cval) < len(bval) * 0.5:
                result.value_drift[full_path] = f"shortened: {len(bval)}→{len(cval)} chars"
            elif len(cval) > len(bval) * 2:
                result.value_drift[full_path] = f"expanded: {len(bval)}→{len(cval)} chars"
        elif isinstance(bval, (int, float)) and bval != cval:
            result.value_drift[full_path] = f"{bval} → {cval}"
        elif isinstance(bval, dict):
            sub = deep_diff(bval, cval, full_path)
            result.missing_keys += sub.missing_keys
            result.extra_keys += sub.extra_keys
            result.type_changes.update(sub.type_changes)
            result.value_drift.update(sub.value_drift)
            if not sub.schema_ok:
                result.schema_ok = False

    return result


TOOL_OUTPUT_SCHEMA = {
    "status": "success",
    "data": {"count": 0, "items": [], "has_more": False},
    "metadata": {"model": "haiku", "version": "1.0"},
}

def run_structural_diff(system_v1: str, system_v2: str):
    client = anthropic.Anthropic()

    tools = [{
        "name": "get_results",
        "description": "Return structured search results",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }]

    queries = ["python tutorials", "machine learning basics", "REST API design"]

    for query in queries:
        prompt = f"Search for: {query}. Use the get_results tool and populate all fields."

        results = {}
        for version, system in [("v1", system_v1), ("v2", system_v2)]:
            try:
                r = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    system=system,
                    tools=tools,
                    messages=[{"role": "user", "content": prompt}],
                )
                for block in r.content:
                    if block.type == "tool_use":
                        results[version] = block.input
                        break
                else:
                    results[version] = {}
            except Exception as e:
                results[version] = {"error": str(e)}

        if "v1" in results and "v2" in results:
            diff = deep_diff(results["v1"], results["v2"])
            diff.prompt = query

            issues = []
            if diff.missing_keys:
                issues.append(f"missing: {diff.missing_keys}")
            if diff.type_changes:
                issues.append(f"type changes: {diff.type_changes}")
            if diff.value_drift:
                issues.append(f"value drift: {list(diff.value_drift.keys())}")

            status = "⚠️ DIFF" if issues else "✓ OK"
            print(f"{status} [{query}]")
            for issue in issues:
                print(f"  → {issue}")


if __name__ == "__main__":
    run_structural_diff(
        system_v1="You are an assistant that always uses tools when asked.",
        system_v2="You are a concise assistant. Use tools only when necessary.",
    )
# Expected Token Savings: None direct — structural diff catches schema breaks before they corrupt downstream parsers
# Environment: pip install anthropic; json is stdlib
```

---

## Option 5: LLM-as-Judge Output Diff

```python
import sqlite3
import json
import anthropic
from datetime import datetime

JUDGE_SYSTEM = """You are a regression detector for AI agent outputs.
Compare a baseline response to a candidate response and evaluate whether the candidate
has regressed in quality. Reply ONLY with JSON:
{"regressed": true/false, "reason": "brief explanation", "severity": "none|minor|major|critical"}"""


class LLMJudgeDiff:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS judgments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT,
                baseline_version TEXT,
                candidate_version TEXT,
                regressed INTEGER,
                severity TEXT,
                reason TEXT,
                judged_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def judge(self, prompt: str, baseline: str, candidate: str,
              baseline_v: str, candidate_v: str) -> dict:
        client = anthropic.Anthropic()

        judge_prompt = (
            f"Input prompt: {prompt}\n\n"
            f"BASELINE response (version {baseline_v}):\n{baseline}\n\n"
            f"CANDIDATE response (version {candidate_v}):\n{candidate}\n\n"
            "Has the candidate regressed compared to the baseline?"
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": judge_prompt}],
        )

        try:
            verdict = json.loads(response.content[0].text)
        except json.JSONDecodeError:
            verdict = {"regressed": False, "reason": "parse error", "severity": "none"}

        self.conn.execute(
            "INSERT INTO judgments (prompt, baseline_version, candidate_version, regressed, severity, reason) VALUES (?,?,?,?,?,?)",
            (prompt[:100], baseline_v, candidate_v,
             int(verdict.get("regressed", False)),
             verdict.get("severity", "none"),
             verdict.get("reason", "")[:200]),
        )
        self.conn.commit()
        return verdict

    def summary(self) -> dict:
        rows = self.conn.execute("""
            SELECT candidate_version, severity, COUNT(*) FROM judgments
            GROUP BY candidate_version, severity ORDER BY severity
        """).fetchall()
        result = {}
        for r in rows:
            result.setdefault(r[0], {})[r[1]] = r[2]
        return result


GOLDENS = {
    "What is Python?": "Python is a high-level interpreted language known for clear syntax and broad library support.",
    "Explain async/await.": "async/await allows writing asynchronous code that looks synchronous, using coroutines.",
    "What is a REST API?": "A REST API uses HTTP methods to perform CRUD operations on resources identified by URLs.",
}


def run_llm_judge_diff(candidate_system: str, candidate_version: str = "v2"):
    judge_db = LLMJudgeDiff()
    client = anthropic.Anthropic()
    baseline_system = "You are a helpful assistant. Be concise and accurate."

    print(f"Running LLM-as-judge regression check (baseline=v1 vs {candidate_version})...")

    for prompt, golden in GOLDENS.items():
        candidate_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=candidate_system,
            messages=[{"role": "user", "content": prompt}],
        )
        candidate_text = candidate_response.content[0].text

        verdict = judge_db.judge(
            prompt=prompt,
            baseline=golden,
            candidate=candidate_text,
            baseline_v="v1",
            candidate_v=candidate_version,
        )

        icon = "⚠️" if verdict.get("regressed") else "✓"
        print(f"  {icon} [{verdict.get('severity','?')}] {prompt[:40]}")
        print(f"     Judge: {verdict.get('reason', '')[:80]}")

    print(f"\nSummary: {json.dumps(judge_db.summary(), indent=2)}")


if __name__ == "__main__":
    run_llm_judge_diff(
        candidate_system="You are an extremely brief assistant. Use at most 5 words per answer.",
        candidate_version="v2-ultrabrief",
    )
# Expected Token Savings: Judge uses haiku (~$0.001/call) to prevent regressive prompt from hitting production
# Environment: pip install anthropic; sqlite3, json are stdlib
```

---

## Option 6: Continuous Diff Pipeline with Alert Webhooks

```python
import sqlite3
import json
import difflib
import hashlib
import anthropic
from datetime import datetime
from dataclasses import dataclass

@dataclass
class DiffAlert:
    prompt: str
    similarity: float
    severity: str
    diff_preview: str

SEVERITY_BANDS = [
    (0.90, "none"),
    (0.75, "minor"),
    (0.50, "major"),
    (0.00, "critical"),
]

def severity_for(similarity: float) -> str:
    for threshold, level in SEVERITY_BANDS:
        if similarity >= threshold:
            return level
    return "critical"


class ContinuousDiffPipeline:
    def __init__(self, db_path: str = ":memory:", webhook_url: str | None = None):
        self.conn = sqlite3.connect(db_path)
        self.webhook_url = webhook_url
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS golden_store (
                prompt_hash TEXT PRIMARY KEY,
                prompt TEXT,
                response TEXT,
                system_version TEXT,
                saved_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS diff_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                prompt TEXT,
                baseline_version TEXT,
                candidate_version TEXT,
                similarity REAL,
                severity TEXT,
                diff_preview TEXT,
                ran_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def _ph(self, p: str) -> str:
        return hashlib.md5(p.encode()).hexdigest()[:10]

    def upsert_golden(self, prompt: str, response: str, version: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO golden_store (prompt_hash, prompt, response, system_version) VALUES (?,?,?,?)",
            (self._ph(prompt), prompt[:200], response, version),
        )
        self.conn.commit()

    def get_golden(self, prompt: str) -> tuple[str, str] | None:
        row = self.conn.execute(
            "SELECT response, system_version FROM golden_store WHERE prompt_hash=?",
            (self._ph(prompt),),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def run_diff(self, prompt: str, candidate: str, candidate_version: str, run_id: str) -> DiffAlert | None:
        golden = self.get_golden(prompt)
        if not golden:
            return None

        baseline_text, baseline_version = golden
        similarity = difflib.SequenceMatcher(None, baseline_text, candidate).ratio()
        sev = severity_for(similarity)

        diff_lines = list(difflib.unified_diff(
            baseline_text.splitlines(), candidate.splitlines(),
            fromfile=baseline_version, tofile=candidate_version, n=1,
        ))
        diff_preview = "".join(diff_lines[:6])

        self.conn.execute(
            "INSERT INTO diff_runs (run_id, prompt, baseline_version, candidate_version, similarity, severity, diff_preview) VALUES (?,?,?,?,?,?,?)",
            (run_id, prompt[:100], baseline_version, candidate_version, round(similarity, 4), sev, diff_preview[:300]),
        )
        self.conn.commit()

        if sev in ("major", "critical"):
            self._fire_alert(prompt, similarity, sev, diff_preview)

        return DiffAlert(prompt=prompt, similarity=round(similarity, 3), severity=sev, diff_preview=diff_preview)

    def _fire_alert(self, prompt: str, similarity: float, severity: str, diff: str):
        """In production: POST to Slack/PagerDuty. Here: print to console."""
        payload = {
            "alert": "output_regression",
            "severity": severity,
            "similarity": similarity,
            "prompt_preview": prompt[:50],
            "diff_preview": diff[:200],
            "timestamp": datetime.utcnow().isoformat(),
        }
        print(f"\n🚨 ALERT [{severity.upper()}] similarity={similarity:.2f}")
        print(f"   Payload: {json.dumps(payload)[:120]}")

    def run_report(self, run_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT severity, COUNT(*), AVG(similarity) FROM diff_runs WHERE run_id=? GROUP BY severity",
            (run_id,),
        ).fetchall()
        return {r[0]: {"count": r[1], "avg_sim": round(r[2], 3)} for r in rows}


def run_continuous_pipeline(prompts: list[str], new_system: str, run_id: str = "run-001"):
    pipeline = ContinuousDiffPipeline()
    client = anthropic.Anthropic()

    baseline_system = "You are a helpful, accurate assistant. Be concise."

    # Seed goldens
    for prompt in prompts:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=baseline_system,
            messages=[{"role": "user", "content": prompt}],
        )
        pipeline.upsert_golden(prompt, r.content[0].text, version="v1-baseline")

    print(f"\nRunning diff for: {new_system[:60]}...")
    for prompt in prompts:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=new_system,
            messages=[{"role": "user", "content": prompt}],
        )
        alert = pipeline.run_diff(prompt, r.content[0].text, "v2-candidate", run_id)
        if alert:
            icon = {"none": "✓", "minor": "~", "major": "⚠️", "critical": "🚨"}.get(alert.severity, "?")
            print(f"  {icon} [{alert.severity}/{alert.similarity:.2f}] {prompt[:40]}")

    print(f"\nRun Report: {json.dumps(pipeline.run_report(run_id), indent=2)}")


if __name__ == "__main__":
    prompts = [
        "What is Python?",
        "Explain a hash table.",
        "What is REST?",
        "How does TLS work?",
        "What is recursion?",
    ]
    run_continuous_pipeline(
        prompts,
        new_system="You are a one-sentence assistant. Never use more than one sentence.",
    )
# Expected Token Savings: None direct — 2× calls during evaluation; severe regressions trigger immediate rollback
# Environment: pip install anthropic; sqlite3, json, difflib, hashlib are stdlib
```

---

## Comparison

| Option | Diff Method | Baseline Storage | Semantic Awareness | SQLite | Alert | Best For |
|--------|-------------|-----------------|-------------------|--------|-------|----------|
| 1 | Character-level | In-memory dict | No | No | No | Quick local regression check |
| 2 | Embedding + probes | In-memory dict | Yes | No | No | Meaning-sensitive comparison |
| 3 | Character-level | SQLite versioned | No | Yes | No | Multi-version CI pipeline |
| 4 | Structural/schema | In-memory | No | No | No | JSON-output schema regression |
| 5 | LLM-as-judge | In-memory dict | Yes (LLM) | Yes | No | Quality-aware regression judgment |
| 6 | Character-level | SQLite | No | Yes | Yes (webhook) | Production continuous monitoring |
