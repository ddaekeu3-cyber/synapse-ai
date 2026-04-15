---
layout: solution
title: "Agent Doesn't Implement Shadow Mode Testing for Prompt Changes"
category: general
description: "Prompt changes are deployed directly to production with no way to compare new vs old output quality before real users are affected."
tags: [general, testing, prompt-engineering, reliability, production]
---

## Symptom

A developer improves the system prompt — tightening instructions, changing tone, or adding examples — and deploys directly to production. Within hours, user complaints surface: the agent is now too terse, refuses valid requests, or produces a different output format that breaks downstream integrations. Rolling back requires another deployment cycle, and there is no data showing what changed or why.

## Root Cause

Prompts are code. Like code, they have regressions. Unlike code, they are hard to unit-test exhaustively because the model's output is probabilistic and context-dependent. Shadow mode (also called shadow testing or dark launch) runs both the old and new prompt on every live request, logs both outputs, and lets you compare them before switching traffic — giving production signal without production risk.

## Fix

### Option 1 — Synchronous shadow call: run both prompts, return old result

```python
import anthropic
import logging
import time

client = anthropic.Anthropic()
logger = logging.getLogger("shadow")
logging.basicConfig(level=logging.INFO)

PROMPT_V1 = "You are a helpful assistant. Be concise."
PROMPT_V2 = "You are a helpful assistant. Be concise and always end with a one-line summary prefixed 'TL;DR:'."

def call_with_prompt(system: str, user_message: str) -> tuple[str, float]:
    start = time.monotonic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text, time.monotonic() - start

def ask_with_shadow(user_message: str) -> str:
    """Return v1 result; log both for comparison."""
    v1_text, v1_latency = call_with_prompt(PROMPT_V1, user_message)
    v2_text, v2_latency = call_with_prompt(PROMPT_V2, user_message)

    logger.info({
        "event":       "shadow_comparison",
        "prompt":      user_message[:80],
        "v1_len":      len(v1_text),
        "v2_len":      len(v2_text),
        "v1_latency":  round(v1_latency, 3),
        "v2_latency":  round(v2_latency, 3),
        "v1_has_tldr": "TL;DR:" in v1_text,
        "v2_has_tldr": "TL;DR:" in v2_text,
    })

    return v1_text  # always return stable v1 to users

result = ask_with_shadow("Explain how HTTPS works.")
print("User sees:", result[:100])
```

**Expected Token Savings:** Shadow calls double token cost temporarily; this is intentional — the data collected prevents costly production regressions that are worse than the shadow overhead.
**Environment:** Low-to-medium traffic agents where doubling requests is acceptable during a testing window.

---

### Option 2 — Async shadow: fire-and-forget v2 to avoid latency impact

```python
import asyncio
import anthropic
import logging
import time

client = anthropic.AsyncAnthropic()
logger = logging.getLogger("shadow")

PROMPT_V1 = "You are a precise data analyst. State facts only."
PROMPT_V2 = "You are a precise data analyst. State facts only. Format numbers with commas."

async def _shadow_call(user_message: str) -> None:
    """Run v2 in the background — result never shown to user."""
    try:
        start = time.monotonic()
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=PROMPT_V2,
            messages=[{"role": "user", "content": user_message}],
        )
        logger.info({
            "event":   "shadow_v2",
            "latency": round(time.monotonic() - start, 3),
            "length":  len(resp.content[0].text),
            "sample":  resp.content[0].text[:100],
        })
    except Exception as exc:
        logger.warning({"event": "shadow_v2_error", "error": str(exc)})

async def ask(user_message: str) -> str:
    """Return v1 immediately; v2 runs concurrently with no latency impact."""
    v1_task     = asyncio.create_task(_call_v1(user_message))
    shadow_task = asyncio.create_task(_shadow_call(user_message))

    v1_result = await v1_task
    # Shadow runs concurrently but we don't wait for it before returning
    asyncio.ensure_future(shadow_task)
    return v1_result

async def _call_v1(user_message: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PROMPT_V1,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text

async def main():
    questions = [
        "What was global GDP in 2023?",
        "How many active AI startups exist?",
        "What is the population of Tokyo?",
    ]
    results = await asyncio.gather(*[ask(q) for q in questions])
    for q, r in zip(questions, results):
        print(f"Q: {q}\nA: {r[:100]}\n")

asyncio.run(main())
```

**Expected Token Savings:** Async fire-and-forget shadow adds zero latency to the user path; v2 tokens are spent in the background as an investment in safe deployment.
**Environment:** Latency-sensitive agents; high-traffic services where blocking on a shadow call is unacceptable.

---

### Option 3 — Sampled shadow: run v2 on X% of requests only

```python
import anthropic
import random
import logging
import time
import threading

client = anthropic.Anthropic()
logger = logging.getLogger("shadow")

PROMPT_V1 = "You are a support agent. Be empathetic and helpful."
PROMPT_V2 = "You are a support agent. Be empathetic, helpful, and always offer one concrete next step."

SHADOW_RATE = 0.20  # shadow 20% of requests

_shadow_stats = {"total": 0, "shadowed": 0, "lock": threading.Lock()}

def _run_shadow_background(user_message: str) -> None:
    try:
        start = time.monotonic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=PROMPT_V2,
            messages=[{"role": "user", "content": user_message}],
        )
        logger.info({
            "event":  "shadow_sample",
            "latency": round(time.monotonic() - start, 3),
            "v2_len":  len(resp.content[0].text),
            "v2_text": resp.content[0].text[:120],
        })
    except Exception as exc:
        logger.warning({"event": "shadow_error", "err": str(exc)})

def ask(user_message: str) -> str:
    with _shadow_stats["lock"]:
        _shadow_stats["total"] += 1
        do_shadow = random.random() < SHADOW_RATE
        if do_shadow:
            _shadow_stats["shadowed"] += 1

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PROMPT_V1,
        messages=[{"role": "user", "content": user_message}],
    )

    if do_shadow:
        t = threading.Thread(target=_run_shadow_background, args=(user_message,), daemon=True)
        t.start()

    return resp.content[0].text

for i in range(20):
    print(ask(f"I need help with issue #{i}.")[:80])

stats = _shadow_stats
print(f"\nShadow stats: {stats['shadowed']}/{stats['total']} requests shadowed")
```

**Expected Token Savings:** Sampling at 20% limits shadow overhead to +20% token cost while still collecting meaningful quality signal over time.
**Environment:** High-traffic production agents where full shadow doubling is cost-prohibitive; gradual rollout experiments.

---

### Option 4 — A/B log comparison: structured diff of v1 vs v2 outputs

```python
import anthropic
import json
import difflib
import logging
from dataclasses import dataclass, asdict

client = anthropic.Anthropic()
logger = logging.getLogger("shadow.diff")

PROMPT_V1 = "Answer briefly in plain English."
PROMPT_V2 = "Answer briefly in plain English. Use bullet points for lists."

@dataclass
class ShadowRecord:
    prompt:   str
    v1_text:  str
    v2_text:  str
    len_diff: int
    added_bullets: bool
    similarity: float

def similarity_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()

def shadow_compare(user_message: str) -> str:
    def call(system: str) -> str:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        return resp.content[0].text

    v1 = call(PROMPT_V1)
    v2 = call(PROMPT_V2)

    record = ShadowRecord(
        prompt        = user_message[:80],
        v1_text       = v1[:200],
        v2_text       = v2[:200],
        len_diff      = len(v2) - len(v1),
        added_bullets = "•" in v2 or "-" in v2.split("\n")[0],
        similarity    = round(similarity_ratio(v1, v2), 3),
    )
    logger.info(json.dumps(asdict(record)))

    # Alert if responses diverge significantly
    if record.similarity < 0.5:
        logger.warning({
            "event":      "shadow_high_divergence",
            "similarity": record.similarity,
            "prompt":     user_message[:60],
        })

    return v1

questions = [
    "What are the main cloud providers?",
    "How do I restart a Kubernetes pod?",
    "What is the difference between TCP and UDP?",
]
for q in questions:
    shadow_compare(q)
```

**Expected Token Savings:** Similarity scoring and structured diffs quantify prompt changes; low similarity alerts catch regressions before they reach users.
**Environment:** Teams running A/B experiments on prompt changes; product analytics pipelines tracking output quality over time.

---

### Option 5 — Shadow with eval scoring: auto-rate output quality

```python
import anthropic
import logging

client = anthropic.Anthropic()
logger = logging.getLogger("shadow.eval")

PROMPT_V1 = "You are a technical writer. Explain concepts clearly."
PROMPT_V2 = "You are a technical writer. Explain concepts clearly. Always include a real-world analogy."

EVAL_SYSTEM = (
    "You are an output quality evaluator. Score the following text on:\n"
    "- clarity (1-5)\n- completeness (1-5)\n- usefulness (1-5)\n"
    'Respond with JSON only: {"clarity": N, "completeness": N, "usefulness": N}'
)

def score_output(text: str) -> dict:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=EVAL_SYSTEM,
            messages=[{"role": "user", "content": f"Rate this text:\n\n{text[:500]}"}],
        )
        import json
        return json.loads(resp.content[0].text)
    except Exception:
        return {"clarity": 0, "completeness": 0, "usefulness": 0}

def shadow_with_eval(user_message: str) -> str:
    def call(system: str) -> str:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        return resp.content[0].text

    v1 = call(PROMPT_V1)
    v2 = call(PROMPT_V2)

    v1_score = score_output(v1)
    v2_score = score_output(v2)

    v1_total = sum(v1_score.values())
    v2_total = sum(v2_score.values())

    logger.info({
        "event":    "shadow_eval",
        "prompt":   user_message[:60],
        "v1_score": v1_total,
        "v2_score": v2_total,
        "v2_wins":  v2_total > v1_total,
        "delta":    v2_total - v1_total,
    })

    return v1  # still return v1 to users during evaluation

for q in ["Explain gradient descent.", "What is a hash table?", "Describe TCP handshake."]:
    shadow_with_eval(q)
```

**Expected Token Savings:** Automated eval scores replace expensive manual human review; data accumulates across requests to build a statistically significant picture of which prompt version wins.
**Environment:** Teams building prompt evaluation pipelines; agents where output quality is measurable (technical accuracy, format compliance, completeness).

---

### Option 6 — Shadow registry: manage multiple prompt experiments simultaneously

```python
import anthropic
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()
logger = logging.getLogger("shadow.registry")

@dataclass
class Experiment:
    name:    str
    v1:      str
    v2:      str
    rate:    float = 0.1
    wins_v2: int   = 0
    total:   int   = 0
    _lock:   threading.Lock = field(default_factory=threading.Lock)

    def record(self, v1_len: int, v2_len: int) -> None:
        with self._lock:
            self.total += 1
            # Simple heuristic: longer response wins (replace with real eval)
            if v2_len > v1_len * 1.1:
                self.wins_v2 += 1

    def win_rate(self) -> float:
        return self.wins_v2 / max(self.total, 1)

class ShadowRegistry:
    def __init__(self):
        self._experiments: dict[str, Experiment] = {}

    def register(self, exp: Experiment) -> None:
        self._experiments[exp.name] = exp

    def ask(self, experiment_name: str, user_message: str) -> str:
        import random
        exp = self._experiments[experiment_name]

        v1_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=exp.v1,
            messages=[{"role": "user", "content": user_message}],
        )
        v1_text = v1_resp.content[0].text

        if random.random() < exp.rate:
            def _shadow():
                v2_resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    system=exp.v2,
                    messages=[{"role": "user", "content": user_message}],
                )
                exp.record(len(v1_text), len(v2_resp.content[0].text))
                logger.info({"experiment": exp.name, "win_rate": exp.win_rate(), "n": exp.total})
            threading.Thread(target=_shadow, daemon=True).start()

        return v1_text

    def report(self) -> None:
        for name, exp in self._experiments.items():
            print(f"  {name}: n={exp.total}, v2_win_rate={exp.win_rate():.0%}")

registry = ShadowRegistry()
registry.register(Experiment("tone_formal",   v1="Be concise.",         v2="Be concise and formal.", rate=0.5))
registry.register(Experiment("add_examples",  v1="Explain clearly.",    v2="Explain with examples.", rate=0.5))

for i in range(10):
    registry.ask("tone_formal",  f"Question {i}: what is containerisation?")
    registry.ask("add_examples", f"Question {i}: what is a load balancer?")

time.sleep(1)  # let shadow threads complete
print("\nExperiment results:")
registry.report()
```

**Expected Token Savings:** Registry manages multiple simultaneous experiments; per-experiment sampling rates let high-risk changes shadow at 5% while low-risk ones run at 50%.
**Environment:** Teams running multiple concurrent prompt experiments; platforms with a prompt management layer.

---

## Comparison

| Option | Shadow Timing | Latency Impact | Sampling | Quality Signal | Best For |
|---|---|---|---|---|---|
| 1. Synchronous | Inline with request | +1 API call latency | 100% | Structured log | Low-traffic agents; initial experiments |
| 2. Async fire-and-forget | Background task | Zero | 100% | Structured log | Latency-sensitive agents |
| 3. Sampled | Background thread | Zero | Configurable % | Structured log | High-traffic; cost-controlled shadow |
| 4. A/B diff | Inline | +1 API call | 100% | Similarity score + diff | Analytics-focused teams |
| 5. Eval scoring | Inline | +2 API calls | 100% | LLM quality scores | Teams building eval pipelines |
| 6. Registry | Background thread | Zero | Per-experiment % | Win rate over time | Multi-experiment management |
