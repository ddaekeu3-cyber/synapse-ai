---
title: "Agent doesn't implement model output fingerprinting"
description: "After a model version update, the agent's outputs silently change in tone, format, or reasoning quality. Without fingerprinting, behavioral regressions go undetected until users complain."
difficulty: intermediate
category: observability
tags: [fingerprinting, behavioral-testing, model-versioning, drift-detection, embeddings]
---

## Problem

When Anthropic releases a new model version or you switch between tiers (`haiku` → `sonnet` → `opus`), the agent's output characteristics change: response length, formatting conventions, reasoning depth, refusal patterns, and factual accuracy all shift. Without fingerprinting, these changes are invisible until users file support tickets.

Model output fingerprinting captures a compact, comparable signature of each output so that:
- Behavioral changes after model updates are detected automatically
- Regressions can be flagged before they reach users
- Output stability guarantees can be measured and reported in SLOs

```python
# BAD: outputs change silently after model bump — no detection
response = client.messages.create(model="claude-sonnet-4-6", ...)
return response.content[0].text  # no fingerprint, no comparison
```

## Solution 1: Lexical fingerprint — normalized hash of output text

Hash a normalized form of the output (lowercased, whitespace-collapsed, punctuation-stripped). Identical semantic content from the same model produces the same hash; model changes produce different hashes.

```python
import hashlib
import re
import json
from dataclasses import dataclass
from datetime import datetime


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)          # strip punctuation
    text = re.sub(r"\s+", " ", text).strip()      # collapse whitespace
    return text


def lexical_fingerprint(text: str) -> str:
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


@dataclass
class OutputRecord:
    prompt_hash: str
    model: str
    fingerprint: str
    length: int
    timestamp: str


class LexicalFingerprintStore:
    def __init__(self, path: str = ".fingerprints.jsonl"):
        self.path = path

    def record(self, prompt: str, model: str, output: str) -> OutputRecord:
        record = OutputRecord(
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()[:12],
            model=model,
            fingerprint=lexical_fingerprint(output),
            length=len(output.split()),
            timestamp=datetime.utcnow().isoformat(),
        )
        with open(self.path, "a") as f:
            f.write(json.dumps(record.__dict__) + "\n")
        return record

    def load_all(self) -> list[OutputRecord]:
        records = []
        try:
            with open(self.path) as f:
                for line in f:
                    records.append(OutputRecord(**json.loads(line)))
        except FileNotFoundError:
            pass
        return records

    def detect_drift(self, prompt: str, model: str, output: str) -> dict:
        new_fp = lexical_fingerprint(output)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]

        history = [
            r for r in self.load_all()
            if r.prompt_hash == prompt_hash and r.model == model
        ]
        if not history:
            return {"status": "no_baseline", "fingerprint": new_fp}

        baseline = history[-1].fingerprint
        changed = baseline != new_fp
        return {
            "status": "drift" if changed else "stable",
            "fingerprint": new_fp,
            "baseline": baseline,
            "prompt_hash": prompt_hash,
        }


# --- Usage ---
store = LexicalFingerprintStore()
prompt = "Summarize the key risks of deploying LLMs in production."
output = "Key risks include hallucination, latency, cost overruns, and prompt injection..."

result = store.detect_drift(prompt, "claude-sonnet-4-6", output)
print(result)
store.record(prompt, "claude-sonnet-4-6", output)
```

## Solution 2: Semantic fingerprint using embedding cosine distance

Embed the output and compare it to a reference embedding. Captures meaning-level drift that survives rewording — catches paraphrases with identical meaning but also flags genuine behavioral shifts.

```python
import asyncio
import numpy as np
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Optional
import json
import os

client = AsyncAnthropic()


async def embed_text(text: str) -> np.ndarray:
    """
    Use a lightweight embedding model. Here we approximate with a hash-based
    fixed vector for demonstration; in production use a real embedding API.
    """
    # Production: call an embedding endpoint (voyage-3, text-embedding-3-small, etc.)
    # For illustration, we derive a pseudo-embedding from character n-grams
    import hashlib
    dim = 128
    vec = np.zeros(dim)
    for i in range(0, len(text) - 2, 1):
        trigram = text[i:i+3]
        h = int(hashlib.md5(trigram.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


@dataclass
class SemanticFingerprint:
    prompt_id: str
    model: str
    embedding: list[float]
    text_length: int


class SemanticFingerprintRegistry:
    def __init__(self, path: str = ".semantic_fingerprints.json"):
        self.path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                self._data = json.load(f)

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f)

    async def register(self, prompt_id: str, model: str, output: str):
        emb = await embed_text(output)
        key = f"{prompt_id}::{model}"
        self._data[key] = {
            "embedding": emb.tolist(),
            "length": len(output.split()),
        }
        self._save()
        return emb

    async def compare(
        self, prompt_id: str, model: str, output: str, threshold: float = 0.95
    ) -> dict:
        key = f"{prompt_id}::{model}"
        if key not in self._data:
            return {"status": "no_baseline"}

        baseline_emb = np.array(self._data[key]["embedding"])
        current_emb = await embed_text(output)
        sim = cosine_similarity(baseline_emb, current_emb)

        return {
            "status": "stable" if sim >= threshold else "semantic_drift",
            "cosine_similarity": round(sim, 4),
            "threshold": threshold,
            "prompt_id": prompt_id,
            "model": model,
        }


# --- Usage ---
async def main():
    registry = SemanticFingerprintRegistry()
    output = "LLM production risks include hallucination, latency spikes, and injection attacks."
    await registry.register("risk_summary", "claude-sonnet-4-6", output)

    new_output = "In production, LLMs can hallucinate facts, suffer from high latency, and face prompt injection."
    result = await registry.compare("risk_summary", "claude-sonnet-4-6", new_output)
    print(result)

asyncio.run(main())
```

## Solution 3: Structural fingerprint — format, length, and key-pattern consistency

Track output structure: response length percentile, presence of headers, bullet count, code block count, and JSON validity. Structural drift often indicates model behavior change even when semantic content is similar.

```python
import re
import json
import statistics
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class StructuralSignature:
    word_count: int
    has_markdown_headers: bool
    bullet_count: int
    code_block_count: int
    is_valid_json: bool
    avg_sentence_length: float
    paragraph_count: int


def structural_signature(text: str) -> StructuralSignature:
    words = text.split()
    headers = len(re.findall(r"^#{1,6}\s", text, re.MULTILINE))
    bullets = len(re.findall(r"^\s*[-*]\s", text, re.MULTILINE))
    code_blocks = len(re.findall(r"```", text)) // 2
    paragraphs = len([p for p in text.split("\n\n") if p.strip()])
    sentences = re.split(r"[.!?]+", text)
    avg_sent_len = statistics.mean(len(s.split()) for s in sentences if s.strip()) if sentences else 0

    try:
        json.loads(text)
        is_json = True
    except Exception:
        is_json = False

    return StructuralSignature(
        word_count=len(words),
        has_markdown_headers=headers > 0,
        bullet_count=bullets,
        code_block_count=code_blocks,
        is_valid_json=is_json,
        avg_sentence_length=round(avg_sent_len, 1),
        paragraph_count=paragraphs,
    )


class StructuralDriftDetector:
    def __init__(self):
        self._baselines: dict[str, list[StructuralSignature]] = {}

    def record_baseline(self, prompt_id: str, sig: StructuralSignature):
        self._baselines.setdefault(prompt_id, []).append(sig)

    def detect(self, prompt_id: str, sig: StructuralSignature) -> dict:
        history = self._baselines.get(prompt_id, [])
        if not history:
            return {"status": "no_baseline"}

        drifted = []
        ref = history[-1]

        # Check boolean flags
        for field in ["has_markdown_headers", "is_valid_json"]:
            if getattr(sig, field) != getattr(ref, field):
                drifted.append(f"{field}: {getattr(ref, field)} → {getattr(sig, field)}")

        # Check numeric fields with tolerance
        for field, tolerance in [
            ("word_count", 0.3),
            ("bullet_count", 0.5),
            ("avg_sentence_length", 0.4),
        ]:
            ref_val = getattr(ref, field)
            cur_val = getattr(sig, field)
            if ref_val == 0 and cur_val == 0:
                continue
            pct_change = abs(cur_val - ref_val) / max(ref_val, 1)
            if pct_change > tolerance:
                drifted.append(f"{field}: {ref_val} → {cur_val} ({pct_change:.0%} change)")

        return {
            "status": "drift" if drifted else "stable",
            "changes": drifted,
            "signature": asdict(sig),
        }


# --- Usage ---
detector = StructuralDriftDetector()
ref_output = "## Summary\n\nKey risks:\n- Hallucination\n- Latency\n- Cost\n\nAddress these with monitoring."
detector.record_baseline("risk_summary", structural_signature(ref_output))

new_output = "Hallucination is a risk. Latency is a risk. Cost is a risk."
result = detector.detect("risk_summary", structural_signature(new_output))
print(result)
```

## Solution 4: Statistical distribution tracker using token-frequency Wasserstein distance

Build a word/token frequency distribution from outputs and measure Wasserstein distance between the reference distribution and current outputs. Catches gradual vocabulary drift.

```python
import re
import math
from collections import Counter
from typing import Optional


def token_distribution(text: str, top_n: int = 200) -> dict[str, float]:
    tokens = re.findall(r"\b\w+\b", text.lower())
    counts = Counter(tokens)
    total = sum(counts.values()) or 1
    top = counts.most_common(top_n)
    return {word: count / total for word, count in top}


def wasserstein_approx(dist_a: dict[str, float], dist_b: dict[str, float]) -> float:
    """
    Approximate 1-D Wasserstein distance between two word distributions.
    Uses the sorted absolute difference of sorted probabilities as a proxy.
    """
    all_keys = sorted(set(dist_a) | set(dist_b))
    vec_a = [dist_a.get(k, 0.0) for k in all_keys]
    vec_b = [dist_b.get(k, 0.0) for k in all_keys]
    # L1 distance between distributions ≈ total variation distance
    return sum(abs(a - b) for a, b in zip(vec_a, vec_b)) / 2.0


class VocabularyDriftTracker:
    def __init__(self, drift_threshold: float = 0.15):
        self.baselines: dict[str, dict[str, float]] = {}
        self.threshold = drift_threshold
        self.history: dict[str, list[float]] = {}

    def update_baseline(self, task_id: str, outputs: list[str]):
        combined = " ".join(outputs)
        self.baselines[task_id] = token_distribution(combined)

    def check(self, task_id: str, output: str) -> dict:
        if task_id not in self.baselines:
            return {"status": "no_baseline"}

        current_dist = token_distribution(output)
        baseline_dist = self.baselines[task_id]
        distance = wasserstein_approx(baseline_dist, current_dist)

        self.history.setdefault(task_id, []).append(distance)

        return {
            "status": "drift" if distance > self.threshold else "stable",
            "wasserstein_distance": round(distance, 4),
            "threshold": self.threshold,
            "rolling_avg": round(
                sum(self.history[task_id][-10:]) / len(self.history[task_id][-10:]), 4
            ),
        }


# --- Usage ---
tracker = VocabularyDriftTracker(drift_threshold=0.15)
ref_outputs = [
    "LLM risks include hallucination and latency.",
    "Production LLMs face hallucination, cost, and injection risks.",
]
tracker.update_baseline("risk_summary", ref_outputs)

new_output = "Generative models exhibit stochastic behaviors leading to factual inconsistencies."
print(tracker.check("risk_summary", new_output))
```

## Solution 5: LLM-judged behavioral equivalence score

Use a lightweight judge model to compare current and reference outputs on a rubric: factual accuracy, completeness, tone, and format adherence. Produces a 0–1 equivalence score.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

JUDGE_PROMPT = """You are an output equivalence judge. Compare two AI responses to the same prompt.

Prompt: {prompt}

Reference response:
{reference}

Current response:
{current}

Score on four dimensions (each 0.0–1.0):
1. factual_accuracy: Are the same facts present and correct?
2. completeness: Does the current response cover all key points from the reference?
3. tone_consistency: Are tone and formality consistent?
4. format_adherence: Does the current response match the reference format (length, structure)?

Respond ONLY with a JSON object:
{{"factual_accuracy": 0.0, "completeness": 0.0, "tone_consistency": 0.0, "format_adherence": 0.0, "overall": 0.0}}"""


async def judge_equivalence(
    prompt: str, reference: str, current: str
) -> dict:
    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": JUDGE_PROMPT.format(
                prompt=prompt, reference=reference, current=current
            ),
        }],
    )
    text = message.content[0].text.strip()
    try:
        scores = json.loads(text)
        scores["status"] = "stable" if scores.get("overall", 0) >= 0.85 else "behavioral_drift"
        return scores
    except json.JSONDecodeError:
        return {"status": "judge_error", "raw": text}


# --- Usage ---
async def main():
    prompt = "What are the main risks of deploying LLMs in production?"
    reference = "Key risks: hallucination, latency, cost overruns, and prompt injection."
    current = "The primary concerns with production LLMs are factual inconsistency, slow response times, budget overruns, and adversarial prompt manipulation."

    result = await judge_equivalence(prompt, reference, current)
    print(json.dumps(result, indent=2))

asyncio.run(main())
```

## Solution 6: Multi-dimensional fingerprint registry with automated alerting

Combine all fingerprint dimensions into a single registry that scores each output across lexical, structural, and judge dimensions, then triggers alerts when the composite score falls below threshold.

```python
import asyncio
import json
import hashlib
import time
from dataclasses import dataclass, asdict
from typing import Callable, Awaitable, Optional


@dataclass
class CompositeFingerprint:
    prompt_id: str
    model: str
    timestamp: float
    lexical_fp: str
    word_count: int
    bullet_count: int
    has_headers: bool
    judge_score: Optional[float]
    composite_score: float
    status: str


def _lexical_fp(text: str) -> str:
    import re
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _structural_score(ref: dict, cur: dict) -> float:
    score = 1.0
    if ref.get("has_headers") != cur.get("has_headers"):
        score -= 0.2
    word_ratio = cur.get("word_count", 0) / max(ref.get("word_count", 1), 1)
    if not (0.7 <= word_ratio <= 1.4):
        score -= 0.3
    return max(0.0, score)


class CompositeFingerprintRegistry:
    def __init__(
        self,
        alert_threshold: float = 0.75,
        on_alert: Optional[Callable[[CompositeFingerprint], Awaitable[None]]] = None,
    ):
        self.threshold = alert_threshold
        self.on_alert = on_alert
        self._baselines: dict[str, dict] = {}
        self._history: list[CompositeFingerprint] = []

    def register_baseline(self, prompt_id: str, model: str, output: str):
        import re
        words = output.split()
        bullets = len(re.findall(r"^\s*[-*]\s", output, re.MULTILINE))
        headers = bool(re.search(r"^#{1,6}\s", output, re.MULTILINE))
        key = f"{prompt_id}::{model}"
        self._baselines[key] = {
            "lexical_fp": _lexical_fp(output),
            "word_count": len(words),
            "bullet_count": bullets,
            "has_headers": headers,
        }

    async def evaluate(
        self,
        prompt_id: str,
        model: str,
        output: str,
        judge_score: Optional[float] = None,
    ) -> CompositeFingerprint:
        import re
        key = f"{prompt_id}::{model}"
        baseline = self._baselines.get(key)

        cur = {
            "lexical_fp": _lexical_fp(output),
            "word_count": len(output.split()),
            "bullet_count": len(re.findall(r"^\s*[-*]\s", output, re.MULTILINE)),
            "has_headers": bool(re.search(r"^#{1,6}\s", output, re.MULTILINE)),
        }

        if baseline is None:
            self.register_baseline(prompt_id, model, output)
            composite_score = 1.0
            status = "baseline_registered"
        else:
            lexical_match = 1.0 if cur["lexical_fp"] == baseline["lexical_fp"] else 0.5
            structural = _structural_score(baseline, cur)
            judge = judge_score if judge_score is not None else 1.0

            composite_score = (lexical_match * 0.2 + structural * 0.3 + judge * 0.5)
            status = "stable" if composite_score >= self.threshold else "drift_alert"

        fp = CompositeFingerprint(
            prompt_id=prompt_id,
            model=model,
            timestamp=time.time(),
            lexical_fp=cur["lexical_fp"],
            word_count=cur["word_count"],
            bullet_count=cur["bullet_count"],
            has_headers=cur["has_headers"],
            judge_score=judge_score,
            composite_score=round(composite_score, 3),
            status=status,
        )
        self._history.append(fp)

        if status == "drift_alert" and self.on_alert:
            await self.on_alert(fp)

        return fp


# --- Usage ---
async def alert_handler(fp: CompositeFingerprint):
    print(f"ALERT: output drift on {fp.prompt_id} | score={fp.composite_score}")


async def main():
    registry = CompositeFingerprintRegistry(
        alert_threshold=0.75, on_alert=alert_handler
    )
    ref = "Key LLM risks: hallucination, latency, cost, injection."
    registry.register_baseline("risk_summary", "claude-sonnet-4-6", ref)

    new = "Generative models introduce unpredictability, performance bottlenecks, and adversarial vulnerabilities."
    fp = await registry.evaluate("risk_summary", "claude-sonnet-4-6", new, judge_score=0.62)
    print(json.dumps(asdict(fp), indent=2))


asyncio.run(main())
```

## Comparison

| Approach | Detects rewording | Cross-model | Requires GPU/API | Interpretable | Latency |
|---|---|---|---|---|---|
| Lexical hash fingerprint | No | Yes | No | Medium | <1 ms |
| Semantic embedding distance | Yes | Yes | Embedding API | Low | 50–200 ms |
| Structural signature | Partial | Yes | No | High | <5 ms |
| Vocabulary Wasserstein | Yes | Yes | No | Medium | <10 ms |
| LLM judge score | Yes | Yes | LLM call | High | 500 ms–2 s |
| Composite multi-dimensional | Yes | Yes | Optional | High | Combined |

**Recommendation**: Run **lexical + structural** fingerprinting (Solutions 1 & 3) on every request — they're free and instant. Run **LLM judge scoring** (Solution 5) on a 5–10% sample and on every post-model-update canary batch. Use the **composite registry** (Solution 6) to gate model version promotions in CI.
