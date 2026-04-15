---
layout: solution
title: "Agent Gives Inconsistent Answers to the Same Question"
category: prompt-engineering
description: "The agent returns different facts, recommendations, or conclusions when the same question is asked twice, undermining user trust and making automated testing impossible."
tags: [prompt-engineering, consistency, temperature, determinism, reliability, testing]
---

## Symptom

A user asks "What is the recommended daily protein intake for an adult?" twice and receives "0.8g per kilogram of body weight" the first time and "50g per day" the second time. Both answers exist in the training data; neither is wrong in isolation; but the inconsistency makes the agent feel unreliable. In automated test suites, the same prompt returns different answers on each run, making assertions flaky and regression testing impossible.

## Root Cause

By default, language models sample from a probability distribution over next tokens. `temperature > 0` introduces randomness: even identical prompts can generate different completions. Additionally, when the model has multiple valid answers to draw from (different units, different framings of the same fact, different phrasings), it will vary between them across calls. High temperature maximises creativity but minimises consistency; the fix depends on whether the inconsistency is random sampling or genuine multi-answer ambiguity.

## Fix

### Option 1 — Set `temperature=0` for factual and deterministic tasks

```python
import anthropic

client = anthropic.Anthropic()

def ask_factual(question: str, deterministic: bool = True) -> str:
    """Ask a factual question with optional determinism."""
    kwargs = {
        "model":    "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": question}],
    }
    if deterministic:
        kwargs["temperature"] = 0   # greedy decoding — maximally consistent

    response = client.messages.create(**kwargs)
    return response.content[0].text

# Demonstrate consistency difference
question = "What is the boiling point of water at sea level in Celsius?"

print("With temperature=0 (deterministic):")
for i in range(3):
    print(f"  Run {i+1}: {ask_factual(question, deterministic=True)[:80]}")

print("\nWith default temperature (stochastic):")
for i in range(3):
    print(f"  Run {i+1}: {ask_factual(question, deterministic=False)[:80]}")

# Classification tasks benefit most from temperature=0
def classify(text: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        temperature=0,
        messages=[{
            "role": "user",
            "content": f"Classify the sentiment as POSITIVE, NEGATIVE, or NEUTRAL. Reply with one word only.\n\nText: {text}",
        }],
    )
    return response.content[0].text.strip().upper()

samples = [
    "This product is amazing, I love it!",
    "Terrible experience, never buying again.",
    "It arrived on time.",
]
for s in samples:
    print(f"  '{s[:40]}' → {classify(s)}")
```

**Expected Token Savings:** `temperature=0` costs no extra tokens and eliminates all sampling-induced inconsistency for factual/classification tasks; removes the need for multi-sample majority voting.
**Environment:** All factual Q&A, classification, extraction, and structured output tasks; use `temperature=0` as the default and increase only for creative generation.

---

### Option 2 — Answer anchoring: provide the canonical answer in the system prompt

```python
import anthropic

client = anthropic.Anthropic()

# Pre-anchor canonical answers for known high-variance questions
CANONICAL_ANSWERS = {
    "protein_intake": "The WHO recommends 0.8g of protein per kilogram of body weight per day for healthy adults.",
    "support_hours":  "Our support team is available Monday–Friday, 9am–6pm UTC.",
    "refund_policy":  "Refunds are available within 30 days of purchase for unused licences.",
    "pricing":        "The Pro plan costs $49/month; the Enterprise plan starts at $299/month.",
}

SYSTEM = f"""You are a helpful assistant for NutraCorp.

Canonical facts — always use these exact figures, never paraphrase or approximate:
- Protein intake recommendation: {CANONICAL_ANSWERS['protein_intake']}
- Support hours: {CANONICAL_ANSWERS['support_hours']}
- Refund policy: {CANONICAL_ANSWERS['refund_policy']}
- Pricing: {CANONICAL_ANSWERS['pricing']}

For other questions, answer accurately and concisely."""

def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        temperature=0,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# Test that canonical answers are used consistently
for question in [
    "How much protein should I eat per day?",
    "What is the recommended daily protein intake?",
    "I heard you should eat 1g of protein per pound — is that what you recommend?",
    "When can I contact support?",
    "What is your refund window?",
]:
    print(f"Q: {question}")
    print(f"A: {ask(question)[:150]}\n")
```

**Expected Token Savings:** Anchored canonical answers prevent multi-turn clarification when users notice contradictions between two agent responses.
**Environment:** Domain-specific agents (support bots, health advisors, pricing agents) where specific facts must be quoted consistently.

---

### Option 3 — Structured output to reduce phrasing variance

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a medical information assistant.
Always return answers in this exact JSON format:
{
  "answer": "<one precise factual sentence>",
  "unit": "<the unit of measurement used, or null>",
  "source_guideline": "<the guideline or organisation this comes from>",
  "confidence": "high" | "moderate" | "low"
}
Do not include any text outside the JSON object."""

def ask_structured(question: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        temperature=0,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"answer": raw, "unit": None, "source_guideline": "unknown", "confidence": "low"}

def format_answer(result: dict) -> str:
    unit = f" ({result['unit']})" if result.get("unit") else ""
    return f"{result['answer']}{unit} [Source: {result.get('source_guideline', 'N/A')}, Confidence: {result.get('confidence')}]"

questions = [
    "What is the recommended daily protein intake for adults?",
    "How much sleep do adults need per night?",
    "What is the recommended daily fibre intake?",
]
for q in questions:
    result = ask_structured(q)
    print(f"Q: {q}")
    print(f"A: {format_answer(result)}\n")

# Consistency check: same question twice
print("Consistency check — same question twice:")
r1 = ask_structured("What is the recommended daily protein intake?")
r2 = ask_structured("What is the recommended daily protein intake?")
print(f"  Run 1: {r1['answer']}")
print(f"  Run 2: {r2['answer']}")
print(f"  Match: {r1['answer'] == r2['answer']}")
```

**Expected Token Savings:** Structured output constrains phrasing variance; the model cannot express the same fact in two different phrasings if it must fit a fixed schema.
**Environment:** Factual Q&A agents where the answer must be machine-comparable across runs; structured output is the strongest consistency guarantee.

---

### Option 4 — Self-consistency sampling: majority vote across N samples

```python
import collections
import anthropic

client = anthropic.Anthropic()

def ask_once(question: str, temperature: float = 0.7) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        temperature=temperature,
        messages=[{"role": "user", "content": question + "\nAnswer with one sentence only."}],
    )
    return response.content[0].text.strip()

def ask_majority_vote(question: str, n: int = 5, temperature: float = 0.5) -> str:
    """
    Sample N responses and return the most common answer.
    Reduces variance for questions with a correct factual answer.
    """
    answers = [ask_once(question, temperature) for _ in range(n)]

    # Normalise: lowercase + strip punctuation for comparison
    def normalise(s: str) -> str:
        return s.lower().strip(".,!?;:")

    normed = [normalise(a) for a in answers]
    majority_normed, count = collections.Counter(normed).most_common(1)[0]

    # Return the original (un-normalised) form of the most common answer
    majority_original = next(a for a, n_ in zip(answers, normed) if n_ == majority_normed)

    print(f"  [vote] n={n}, majority_count={count}/{n}")
    for a in answers:
        marker = "✓" if normalise(a) == majority_normed else " "
        print(f"    {marker} {a[:80]}")

    return majority_original

questions = [
    "How many planets are in the solar system?",
    "What is the chemical symbol for gold?",
    "In what year did World War II end?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask_majority_vote(q)}\n")
```

**Expected Token Savings:** Self-consistency uses N × (base tokens) but only makes sense when `temperature=0` is unavailable or when sampling diversity is intentional; for factual tasks, `temperature=0` is cheaper.
**Environment:** Tasks where `temperature=0` produces wrong answers due to overconfidence, or reasoning tasks where diversity of thought paths improves accuracy.

---

### Option 5 — Answer caching for identical questions

```python
import hashlib
import time
import anthropic

client = anthropic.Anthropic()

class ConsistentAnswerCache:
    """
    Cache answers to seen questions.
    Identical questions always return identical answers.
    """
    def __init__(self, ttl_seconds: int = 3600):
        self._cache: dict[str, tuple[str, float]] = {}
        self.ttl = ttl_seconds
        self.hits = 0
        self.misses = 0

    def _key(self, question: str, system: str) -> str:
        payload = f"{system}||{question.strip().lower()}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def get(self, question: str, system: str) -> str | None:
        key = self._key(question, system)
        entry = self._cache.get(key)
        if entry is None:
            self.misses += 1
            return None
        answer, ts = entry
        if time.time() - ts > self.ttl:
            del self._cache[key]
            self.misses += 1
            return None
        self.hits += 1
        return answer

    def set(self, question: str, system: str, answer: str) -> None:
        key = self._key(question, system)
        self._cache[key] = (answer, time.time())

    @property
    def stats(self) -> str:
        total = self.hits + self.misses
        rate  = self.hits / total * 100 if total else 0
        return f"hits={self.hits} misses={self.misses} hit_rate={rate:.0f}%"

cache  = ConsistentAnswerCache(ttl_seconds=3600)
SYSTEM = "You are a factual Q&A assistant. Answer concisely."

def ask(question: str) -> tuple[str, bool]:
    cached = cache.get(question, SYSTEM)
    if cached is not None:
        return cached, True  # (answer, from_cache)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        temperature=0,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    answer = response.content[0].text
    cache.set(question, SYSTEM, answer)
    return answer, False

questions = [
    "What is the speed of light?",
    "What is the speed of light?",   # cache hit
    "How many bones are in the human body?",
    "What is the speed of light?",   # cache hit again
    "How many bones are in the human body?",   # cache hit
]
for q in questions:
    answer, from_cache = ask(q)
    source = "CACHE" if from_cache else "API  "
    print(f"[{source}] Q: {q[:45]} → {answer[:60]}")

print(f"\nCache stats: {cache.stats}")
```

**Expected Token Savings:** Cache hit costs zero API tokens; questions asked more than once never incur API cost after the first call; identical answers guaranteed for cached questions.
**Environment:** FAQ-style agents, customer support bots, or any agent where the same questions recur across users.

---

### Option 6 — Consistency test harness for regression detection

```python
import json
import hashlib
import anthropic

client = anthropic.Anthropic()

class ConsistencyTestHarness:
    """
    Records expected answers and detects regressions when answers change.
    Run in CI to catch prompt changes that alter factual outputs.
    """

    def __init__(self, baseline_path: str = ".answer_baseline.json"):
        self.path    = baseline_path
        self.baseline: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self) -> None:
        with open(self.path, "w") as f:
            json.dump(self.baseline, f, indent=2)

    def _key(self, system: str, question: str) -> str:
        return hashlib.sha256(f"{system}||{question}".encode()).hexdigest()[:12]

    def ask(self, system: str, question: str) -> str:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text.strip()

    def record(self, system: str, questions: list[str]) -> None:
        """Record baseline answers."""
        for q in questions:
            answer = self.ask(system, q)
            self.baseline[self._key(system, q)] = answer
            print(f"  [record] {q[:50]} → {answer[:60]}")
        self._save()
        print(f"Baseline saved to {self.path}")

    def verify(self, system: str, questions: list[str]) -> list[dict]:
        """Check current answers against baseline. Returns list of failures."""
        failures = []
        for q in questions:
            key     = self._key(system, q)
            current = self.ask(system, q)
            expected = self.baseline.get(key)
            if expected is None:
                print(f"  [skip] no baseline for: {q[:50]}")
                continue
            if current.lower().strip() != expected.lower().strip():
                failures.append({"question": q, "expected": expected, "actual": current})
                print(f"  [FAIL] {q[:50]}")
                print(f"         expected: {expected[:80]}")
                print(f"         actual:   {current[:80]}")
            else:
                print(f"  [pass] {q[:50]}")
        return failures

SYSTEM = "You are a factual assistant. Answer in one sentence."
QUESTIONS = [
    "What is the capital of France?",
    "How many days are in a leap year?",
    "What is the chemical formula for water?",
]

harness = ConsistencyTestHarness()

# First run: record baseline
print("Recording baseline...")
harness.record(SYSTEM, QUESTIONS)

# Subsequent runs: verify consistency
print("\nVerifying consistency...")
failures = harness.verify(SYSTEM, QUESTIONS)
print(f"\nResult: {len(failures)} failure(s) out of {len(QUESTIONS)} questions")
if not failures:
    print("All answers are consistent with baseline.")
```

**Expected Token Savings:** Consistency harness catches answer regressions in CI before they reach production; each CI run costs N × (small token count) and prevents multi-turn correction costs in production.
**Environment:** Agents deployed with CI/CD; baseline recording runs once after prompt changes, verification runs on every deployment.

---

## Comparison

| Option | Cause Addressed | Extra Cost | Consistency Guarantee | Best For |
|---|---|---|---|---|
| 1. `temperature=0` | Sampling randomness | None | Near-deterministic | All factual/classification tasks |
| 2. Answer anchoring | Multi-valid-answer ambiguity | None | Hard (for anchored facts) | Domain agents with canonical facts |
| 3. Structured output | Phrasing variance | None | Schema-level | Machine-readable outputs |
| 4. Majority vote | High-variance tasks | N× tokens | Statistical | Reasoning tasks where `temperature=0` is wrong |
| 5. Answer caching | Repeated identical questions | None after first | Absolute | FAQ agents, high repeat-rate questions |
| 6. Consistency test harness | Regression detection | CI tokens | Verified vs baseline | CI/CD pipelines, prompt versioning |
