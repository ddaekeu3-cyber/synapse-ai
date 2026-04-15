---
layout: solution
title: "Agent Doesn't Implement Automated Prompt Regression Testing"
category: general
description: "Prompt changes are merged without any automated test that verifies the new prompt still produces correct output on a representative set of inputs, so regressions are discovered by users rather than CI."
tags: [general, testing, prompt-engineering, regression, ci]
---

## Symptom

A developer improves the system prompt — better instructions, tighter constraints, new examples — and runs the agent manually on two or three test cases. It looks fine. After merging, a subset of real user queries start producing wrong output: the format changed, a crucial instruction was accidentally overridden, or an edge case the developer didn't test is now broken. The regression is discovered hours or days later through user complaints.

## Root Cause

Unlike code, prompts cannot be type-checked or linted. Their behavior is defined by the model's probabilistic output across the distribution of real user inputs. Without a fixed suite of input–output pairs that must remain stable across prompt changes, any merge is a blind change. Prompt regression tests are snapshot tests for model behavior: they run the prompt against known inputs and assert that outputs satisfy invariants — not that they are character-for-character identical, but that they meet quality criteria.

## Fix

### Option 1 — Snapshot test: assert output matches expected pattern

```python
import anthropic
import re
import json
import os

client = anthropic.Anthropic()

SYSTEM_PROMPT = "You are a concise assistant. Answer in 1–2 sentences."

# Test cases: (input, expected_pattern)
REGRESSION_CASES = [
    ("What is Python?",     r"programming language"),
    ("What is 2 + 2?",      r"\b4\b"),
    ("Capital of France?",  r"Paris"),
    ("What is DNS?",        r"domain name"),
    ("What is HTTP?",       r"protocol|transfer"),
]

def run_regression_test(cases: list[tuple[str, str]], system: str) -> dict:
    results = {"passed": 0, "failed": 0, "failures": []}
    for prompt, pattern in cases:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text  = resp.content[0].text
        match = bool(re.search(pattern, text, re.IGNORECASE))
        if match:
            results["passed"] += 1
            print(f"[pass] {prompt!r}")
        else:
            results["failed"] += 1
            results["failures"].append({"prompt": prompt, "pattern": pattern, "got": text[:100]})
            print(f"[FAIL] {prompt!r}\n  expected pattern: {pattern}\n  got: {text[:100]}")
    return results

if __name__ == "__main__":
    results = run_regression_test(REGRESSION_CASES, SYSTEM_PROMPT)
    total = results["passed"] + results["failed"]
    print(f"\n{results['passed']}/{total} passed")
    if results["failed"] > 0:
        exit(1)  # non-zero exit fails CI
```

**Expected Token Savings:** Running 10 haiku test cases in CI costs ~0.002 USD; catching a prompt regression before merge prevents the full cost of user-reported bugs and rollback cycles.
**Environment:** Any agent with a system prompt; CI pipelines that run on every pull request touching prompt files.

---

### Option 2 — Behavioral assertion suite: test structure, not exact text

```python
import anthropic
import json
import re
import pytest

client = anthropic.Anthropic()

SYSTEM = (
    "You are a structured data extractor. Given a product description, "
    "respond with a JSON object containing: name (string), price_usd (number), "
    "in_stock (boolean), features (list of strings, max 5)."
)

TEST_INPUTS = [
    "The ProBook laptop costs $999.99 and is currently in stock. It has 16GB RAM, SSD storage, and a 15-inch display.",
    "Blue wireless headphones, $49, out of stock. Features: noise cancellation, 20hr battery.",
    "USB-C hub $29.99, available. Ports: HDMI, USB 3.0, SD card, Ethernet.",
]

def assert_extraction(text: str, input_desc: str) -> list[str]:
    """Returns list of assertion failures."""
    failures = []
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return [f"Not valid JSON: {text[:100]}"]

    if not isinstance(data.get("name"), str) or not data["name"]:
        failures.append("'name' missing or not a string")
    if not isinstance(data.get("price_usd"), (int, float)) or data["price_usd"] <= 0:
        failures.append("'price_usd' missing or not a positive number")
    if not isinstance(data.get("in_stock"), bool):
        failures.append("'in_stock' missing or not a boolean")
    features = data.get("features", [])
    if not isinstance(features, list):
        failures.append("'features' is not a list")
    elif len(features) > 5:
        failures.append(f"'features' has {len(features)} items (max 5)")
    return failures

@pytest.mark.parametrize("description", TEST_INPUTS)
def test_extraction_structure(description):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": description}],
    )
    failures = assert_extraction(resp.content[0].text, description)
    assert not failures, f"Assertion failures for {description!r}: {failures}"

@pytest.mark.parametrize("description", TEST_INPUTS)
def test_extraction_price_positive(description):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": description}],
    )
    data = json.loads(resp.content[0].text.strip())
    assert data.get("price_usd", 0) > 0, "Price must be positive"
```

**Expected Token Savings:** Behavioral assertions (structure, types, value constraints) are more stable than string matching; they catch real regressions without failing on harmless rephrasing.
**Environment:** Structured output agents; extraction pipelines where schema stability matters more than exact wording.

---

### Option 3 — Regression test with golden file comparison

```python
import anthropic
import json
import os
import hashlib

client = anthropic.Anthropic()

GOLDEN_DIR    = ".prompt_golden"
SYSTEM_PROMPT = "You are a helpful assistant. Answer questions concisely."

TEST_CASES = [
    {"id": "python_def",   "prompt": "Define Python in one sentence."},
    {"id": "tcp_def",      "prompt": "What is TCP? One sentence."},
    {"id": "api_def",      "prompt": "What is an API? One sentence."},
]

def golden_path(case_id: str) -> str:
    return os.path.join(GOLDEN_DIR, f"{case_id}.json")

def record_goldens() -> None:
    """Run once to create golden files (commit to repo)."""
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for case in TEST_CASES:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": case["prompt"]}],
        )
        text = resp.content[0].text
        with open(golden_path(case["id"]), "w") as f:
            json.dump({"prompt": case["prompt"], "response": text}, f, indent=2)
        print(f"[golden] recorded: {case['id']}")

def run_golden_regression() -> bool:
    """Compare current output against golden files. Returns True if all pass."""
    all_pass = True
    for case in TEST_CASES:
        path = golden_path(case["id"])
        if not os.path.exists(path):
            print(f"[golden] MISSING: {path} — run record_goldens() first")
            all_pass = False
            continue
        with open(path) as f:
            golden = json.load(f)

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": case["prompt"]}],
        )
        current = resp.content[0].text

        # Semantic similarity check: share ≥60% of significant words
        def sig_words(t: str) -> set:
            return {w.lower() for w in t.split() if len(w) > 3}
        golden_words  = sig_words(golden["response"])
        current_words = sig_words(current)
        overlap = len(golden_words & current_words) / max(len(golden_words), 1)

        if overlap >= 0.6:
            print(f"[golden] pass {case['id']!r} (overlap={overlap:.0%})")
        else:
            print(f"[golden] FAIL {case['id']!r} (overlap={overlap:.0%})")
            print(f"  golden:  {golden['response'][:80]}")
            print(f"  current: {current[:80]}")
            all_pass = False
    return all_pass

if __name__ == "__main__":
    import sys
    if "--record" in sys.argv:
        record_goldens()
    else:
        success = run_golden_regression()
        sys.exit(0 if success else 1)
```

**Expected Token Savings:** Golden files create a concrete baseline; word-overlap comparison is more stable than exact matching while still detecting meaningful drift.
**Environment:** CI pipelines; teams using git to track golden files alongside prompt changes for reviewer-visible diffs.

---

### Option 4 — LLM-as-judge regression evaluator

```python
import anthropic
import json

client = anthropic.Anthropic()

SYSTEM_UNDER_TEST = "You are a customer support agent. Be helpful, empathetic, and professional."

EVAL_SYSTEM = """You are an evaluator. Given a customer query and an agent response,
score the response on three criteria (1–5 each):
- helpfulness: Does it address the customer's need?
- tone: Is it empathetic and professional?
- completeness: Does it provide a complete answer?

Respond with JSON only: {"helpfulness": N, "tone": N, "completeness": N, "overall": N}"""

TEST_CASES = [
    "My order hasn't arrived after 2 weeks. I'm very frustrated.",
    "How do I cancel my subscription?",
    "I was charged twice for the same item.",
]

PASS_THRESHOLD = 3.5  # minimum average score to pass

def get_agent_response(query: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_UNDER_TEST,
        messages=[{"role": "user", "content": query}],
    )
    return resp.content[0].text

def evaluate_response(query: str, response: str) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=EVAL_SYSTEM,
        messages=[{"role": "user", "content": f"Query: {query}\n\nResponse: {response}"}],
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except json.JSONDecodeError:
        return {"helpfulness": 0, "tone": 0, "completeness": 0, "overall": 0}

def run_llm_judge_regression() -> bool:
    all_pass = True
    for query in TEST_CASES:
        response = get_agent_response(query)
        scores   = evaluate_response(query, response)
        avg = (scores.get("helpfulness", 0) + scores.get("tone", 0) + scores.get("completeness", 0)) / 3
        passed = avg >= PASS_THRESHOLD
        status = "pass" if passed else "FAIL"
        print(f"[eval] {status} {query!r[:50]}: avg={avg:.1f} {scores}")
        if not passed:
            all_pass = False
    return all_pass

if __name__ == "__main__":
    import sys
    success = run_llm_judge_regression()
    sys.exit(0 if success else 1)
```

**Expected Token Savings:** LLM-as-judge evaluation uses ~3× haiku calls per test case (agent + evaluator); detects quality regressions that regex patterns miss — especially tone and helpfulness degradation.
**Environment:** Support agents, coaching bots, and conversational agents where quality is subjective; complement to structural tests.

---

### Option 5 — Regression test runner with result persistence and trend tracking

```python
import anthropic
import json
import os
import re
import time
from datetime import datetime

client = anthropic.Anthropic()

SYSTEM = "Respond with a JSON object. Fields: answer (string), confidence (high/medium/low)."

CASES = [
    {"id": "cap_france",   "prompt": "Capital of France?",    "pattern": r'"answer":\s*"Paris"'},
    {"id": "py_year",      "prompt": "When was Python created?", "pattern": r'"answer":\s*"199[0-9]'},
    {"id": "tcp_protocol", "prompt": "Is TCP connection-oriented?", "pattern": r'"answer":\s*"[Yy]es'},
]

HISTORY_FILE = ".prompt_regression_history.jsonl"

def run_case(case: dict, system: str) -> dict:
    start = time.monotonic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=system,
        messages=[{"role": "user", "content": case["prompt"]}],
    )
    text    = resp.content[0].text
    latency = time.monotonic() - start
    passed  = bool(re.search(case["pattern"], text))
    return {
        "id":      case["id"],
        "passed":  passed,
        "text":    text[:100],
        "tokens":  resp.usage.input_tokens + resp.usage.output_tokens,
        "latency": round(latency, 3),
        "ts":      datetime.utcnow().isoformat(),
    }

def save_run(results: list[dict], system_hash: str) -> None:
    record = {
        "system_hash": system_hash,
        "ts": datetime.utcnow().isoformat(),
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "results": results,
    }
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

def run_all(system: str) -> bool:
    import hashlib
    system_hash = hashlib.md5(system.encode()).hexdigest()[:8]
    results = [run_case(c, system) for c in CASES]
    save_run(results, system_hash)

    passed = sum(1 for r in results if r["passed"])
    total  = len(results)
    print(f"\n=== Regression Results (prompt hash: {system_hash}) ===")
    for r in results:
        status = "pass" if r["passed"] else "FAIL"
        print(f"  [{status}] {r['id']}: {r['text'][:60]}")
    print(f"\n{passed}/{total} passed")

    # Show trend if history exists
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            runs = [json.loads(line) for line in f if line.strip()]
        if len(runs) > 1:
            prev = runs[-2]
            delta = passed - prev["passed"]
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            print(f"Trend: {prev['passed']}/{prev['total']} → {passed}/{total} {arrow}")

    return passed == total

if __name__ == "__main__":
    import sys
    success = run_all(SYSTEM)
    sys.exit(0 if success else 1)
```

**Expected Token Savings:** Historical trend tracking makes prompt regressions visible over time; system_hash links each run to a specific prompt version so regressions are traceable to the causative change.
**Environment:** Teams with multiple prompt versions in git; CI systems that track metric trends across builds.

---

### Option 6 — Pytest plugin style: mark tests as prompt regression tests

```python
import anthropic
import re
import pytest

client = anthropic.Anthropic()

# ── system prompt under test ───────────────────────────────────────────────────

SYSTEM = (
    "You are a technical glossary. Define terms in exactly 1 sentence. "
    "Always start with the term name followed by 'is' or 'are'."
)

# ── helper ─────────────────────────────────────────────────────────────────────

def query(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

# ── prompt regression markers ──────────────────────────────────────────────────

pytestmark = pytest.mark.prompt_regression  # mark all tests in this module

# ── format invariants ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("term", ["API", "DNS", "TCP", "HTTP", "JSON"])
def test_starts_with_term(term):
    result = query(f"Define {term}.")
    assert result.strip().startswith(term), f"Response must start with '{term}': {result[:80]}"

@pytest.mark.parametrize("term", ["API", "DNS", "TCP"])
def test_contains_is_or_are(term):
    result = query(f"Define {term}.")
    assert re.search(r"\b(is|are)\b", result), f"Response must contain 'is' or 'are': {result[:80]}"

@pytest.mark.parametrize("term", ["API", "HTTP"])
def test_single_sentence(term):
    result = query(f"Define {term}.")
    sentences = [s.strip() for s in re.split(r"[.!?]", result) if s.strip()]
    assert len(sentences) <= 2, f"Expected 1 sentence, got {len(sentences)}: {result[:80]}"

# ── content invariants ─────────────────────────────────────────────────────────

def test_api_mentions_interface_or_protocol():
    result = query("Define API.")
    assert re.search(r"interface|protocol|application", result, re.IGNORECASE), result

def test_dns_mentions_domain_or_name():
    result = query("Define DNS.")
    assert re.search(r"domain|name", result, re.IGNORECASE), result

# Run with: pytest -m prompt_regression -v
# CI: pytest -m prompt_regression --tb=short
```

**Expected Token Savings:** Marking tests as `prompt_regression` allows selective CI runs (only when prompt files change) vs full runs; reduces token spend by skipping regression tests on non-prompt changes.
**Environment:** Mono-repos where prompt changes are a subset of all changes; teams using pytest markers for selective test execution in CI.

---

## Comparison

| Option | Assertion Style | Deterministic | LLM Cost | CI Ready | Best For |
|---|---|---|---|---|---|
| 1. Regex pattern match | Structural pattern | Near | Low | Yes | Simple keyword/value invariants |
| 2. Behavioral assertions | Type + schema | Near | Low | Yes | Structured output agents |
| 3. Golden file comparison | Word overlap similarity | Near | Low | Yes | Tracking drift from known-good baseline |
| 4. LLM-as-judge | Quality scores | No | Medium | Threshold-based | Tone/helpfulness; subjective quality |
| 5. Trend history | Pattern + history | Near | Low | Yes | Multi-version tracking; regression bisect |
| 6. pytest markers | Format + content | Near | Low | Yes | Monorepos; selective CI on prompt changes |
