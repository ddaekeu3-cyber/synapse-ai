---
layout: solution
title: "Agent Response Too Terse — Omits Critical Detail"
category: prompt-engineering
description: "Agent gives one-line answers to questions that require nuanced explanation, skipping edge cases, caveats, prerequisites, and context that the user needs to act correctly."
tags: [prompt-engineering, verbosity, output-quality, system-prompt, few-shot, completeness]
---

## Symptom

A developer asks "How do I handle authentication in FastAPI?" and the agent replies: "Use OAuth2PasswordBearer." The user implements it, ships to production, and discovers they forgot token expiry, refresh logic, and HTTPS enforcement — all critical omissions. The agent's short answer was technically correct but dangerously incomplete. Users report the agent "seems helpful but keeps leaving things out."

## Root Cause

Brevity instructions ("be concise", "answer in one sentence", low `max_tokens`) intended to reduce verbosity on simple queries over-apply to complex ones. The model, having been trained on feedback that rewards short answers, defaults to the minimum viable response. Without explicit instructions to cover edge cases, caveats, and prerequisites, the model omits them — not because it doesn't know them, but because nothing in the prompt signals their importance.

## Fix

### Option 1 — Completeness instruction: require caveats and prerequisites

```python
import anthropic

client = anthropic.Anthropic()

# Terse system prompt — model defaults to short answers
TERSE_SYSTEM = "Answer questions concisely."

# Completeness-focused system prompt
COMPLETE_SYSTEM = """You are a precise technical assistant.

For every technical answer:
1. Give the direct answer first.
2. List prerequisites (what must be true for this answer to apply).
3. List the top 2-3 caveats or edge cases the user must know.
4. If there are common mistakes, mention them briefly.

Skip these sections only if the question is purely factual with no meaningful caveats."""

questions = [
    "How do I handle authentication in FastAPI?",
    "Should I use asyncio.gather for parallel API calls?",
    "Is it safe to store secrets in environment variables?",
]

import anthropic

client = anthropic.Anthropic()

print("Terse system prompt:")
for q in questions:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=TERSE_SYSTEM,
        messages=[{"role": "user", "content": q}],
    )
    print(f"  Q: {q}")
    print(f"  A: {r.content[0].text.strip()[:120]}\n")

print("Completeness-focused prompt:")
for q in questions:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=COMPLETE_SYSTEM,
        messages=[{"role": "user", "content": q}],
    )
    print(f"  Q: {q}")
    print(f"  A: {r.content[0].text.strip()[:400]}\n")
```

**Expected Token Savings:** Completeness instruction increases output by 100-300 tokens per complex answer but prevents users from acting on incomplete advice — a single mis-implementation caught here saves hours of debugging and potential security incidents.
**Environment:** Developer assistants, architecture advisors, and security agents where incomplete answers cause downstream harm; completeness instruction is the minimum viable fix.

---

### Option 2 — Depth selector: route by question complexity to right verbosity

```python
import json
import anthropic

client = anthropic.Anthropic()

DEPTH_CLASSIFIER = """Classify how much depth this question requires.
shallow: one-liner, lookup, definition, "what is X"
medium: how-to with 2-3 steps, explanation with minor caveats
deep: architecture/security/tradeoff questions with significant edge cases
Return JSON: {"depth": "shallow"|"medium"|"deep", "reason": "..."}"""

SYSTEMS = {
    "shallow": "Answer in one sentence. No preamble.",
    "medium":  "Answer in 3-5 sentences. Include the key caveat if there is one.",
    "deep": """Give a thorough answer structured as:
**Answer:** (2-3 sentences)
**Prerequisites:** (bulleted list)
**Key Caveats:** (bulleted list, 3-5 items)
**Common Mistakes:** (1-2 most frequent errors)""",
}

MAX_TOKENS = {"shallow": 32, "medium": 150, "deep": 600}

def classify_depth(question: str) -> str:
    r   = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=DEPTH_CLASSIFIER,
        messages=[{"role": "user", "content": question}],
    )
    raw = r.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw).get("depth", "medium")
    except json.JSONDecodeError:
        return "medium"

def ask(question: str) -> str:
    depth  = classify_depth(question)
    system = SYSTEMS[depth]
    tokens = MAX_TOKENS[depth]
    print(f"  [depth={depth}]")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=tokens,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text.strip()

questions = [
    "What does HTTP stand for?",                                         # shallow
    "What is the difference between list and tuple in Python?",          # medium
    "How should I design authentication for a multi-tenant SaaS app?",  # deep
    "Should I use Redis or Postgres for session storage in production?", # deep
    "What is a decorator?",                                              # shallow
]

for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:500]}\n")
```

**Expected Token Savings:** Depth routing keeps simple answers short (32 tokens) and reserves verbosity for questions that genuinely need it; average token cost drops by 40% compared to always using a deep template, while completeness is preserved where it matters.
**Environment:** General-purpose assistants handling mixed queries; routing ensures neither over-explanation of simple facts nor under-explanation of complex topics.

---

### Option 3 — Few-shot examples showing the right level of completeness

```python
import anthropic

client = anthropic.Anthropic()

# Few-shot examples demonstrating desired completeness level
FEW_SHOT_MESSAGES = [
    {
        "role": "user",
        "content": "How do I read a file in Python?"
    },
    {
        "role": "assistant",
        "content": """Use `open()` with a context manager:

```python
with open("file.txt", "r", encoding="utf-8") as f:
    content = f.read()
```

**Prerequisites:** File must exist; you need read permission on it.
**Caveats:**
- Always specify `encoding="utf-8"` — omitting it uses the OS default, which differs between systems.
- For large files, use `f.readline()` or iterate `for line in f:` instead of `f.read()` to avoid loading the entire file into memory.
- `open()` without a context manager risks leaving the file handle open if an exception occurs."""
    },
    {
        "role": "user",
        "content": "How do I connect to PostgreSQL in Python?"
    },
    {
        "role": "assistant",
        "content": """Use `psycopg2` (sync) or `asyncpg` (async):

```python
import psycopg2
conn = psycopg2.connect(
    host="localhost", dbname="mydb", user="user", password="secret"
)
```

**Prerequisites:** `pip install psycopg2-binary`; PostgreSQL server must be running and accepting connections.
**Caveats:**
- Never interpolate values into SQL strings — use parameterised queries (`cursor.execute("SELECT * FROM t WHERE id = %s", (id,))`) to prevent SQL injection.
- Connection objects are not thread-safe; use a connection pool (`psycopg2.pool.ThreadedConnectionPool`) in multi-threaded applications.
- Always call `conn.close()` or use a context manager to avoid connection leaks."""
    },
]

def ask_with_few_shot(question: str) -> str:
    messages = FEW_SHOT_MESSAGES + [{"role": "user", "content": question}]
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system="You are a precise technical assistant. Follow the format shown in the examples.",
        messages=messages,
    )
    return r.content[0].text.strip()

questions = [
    "How do I send an HTTP POST request in Python?",
    "How do I run background tasks in FastAPI?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask_with_few_shot(q)[:600]}\n")
```

**Expected Token Savings:** Few-shot examples are the most reliable way to enforce a specific completeness format; the model mirrors the demonstrated structure — prerequisites, caveats, and code — without requiring a lengthy system prompt explanation of every rule.
**Environment:** Domain-specific assistants (developer docs, security guides) where the desired completeness pattern is consistent and well-defined; few-shot examples anchor format more reliably than instructions alone.

---

### Option 4 — Self-critique pass: agent reviews its own answer for omissions

```python
import anthropic

client = anthropic.Anthropic()

ANSWER_SYSTEM = "Answer the technical question accurately."

CRITIQUE_SYSTEM = """You are a strict technical reviewer.
Given a question and a draft answer, identify missing information:
- Are prerequisites stated?
- Are security implications covered?
- Are common failure modes mentioned?
- Are important edge cases omitted?

Return JSON: {"missing": ["item1", "item2", ...], "rating": "complete"|"partial"|"incomplete"}
If nothing is missing, return {"missing": [], "rating": "complete"}."""

IMPROVE_SYSTEM = """You are a technical assistant improving a draft answer.
Add the missing items identified by the reviewer.
Preserve the original answer and append only what is missing, clearly labelled."""

def ask_with_self_critique(question: str) -> str:
    import json

    # Step 1: initial answer
    r1 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=ANSWER_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    draft = r1.content[0].text.strip()
    print(f"  Draft ({r1.usage.output_tokens} tok): {draft[:100]}...")

    # Step 2: critique
    critique_prompt = f"Question: {question}\n\nDraft answer:\n{draft}"
    r2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=CRITIQUE_SYSTEM,
        messages=[{"role": "user", "content": critique_prompt}],
    )
    raw = r2.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        critique = json.loads(raw)
    except json.JSONDecodeError:
        critique = {"missing": [], "rating": "complete"}
    print(f"  Critique: rating={critique['rating']} missing={critique['missing']}")

    # Step 3: improve only if incomplete
    if critique["rating"] == "complete" or not critique["missing"]:
        return draft

    improve_prompt = (
        f"Question: {question}\n\nDraft:\n{draft}\n\n"
        f"Missing items identified by reviewer:\n"
        + "\n".join(f"- {m}" for m in critique["missing"])
    )
    r3 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=IMPROVE_SYSTEM,
        messages=[{"role": "user", "content": improve_prompt}],
    )
    return r3.content[0].text.strip()

questions = [
    "How do I hash passwords in Python?",
    "How do I use environment variables in Docker?",
]
for q in questions:
    print(f"Q: {q}")
    final = ask_with_self_critique(q)
    print(f"Final answer:\n{final[:600]}\n")
```

**Expected Token Savings:** Self-critique adds 1-2 extra API calls (~300 tokens) but catches critical omissions automatically; for security-sensitive answers (auth, crypto, input validation), the cost of a missed caveat far exceeds the cost of a critique pass.
**Environment:** Security-critical agents, compliance assistants, and any agent where an incomplete answer can cause a harmful outcome; self-critique is most valuable for high-stakes questions.

---

### Option 5 — Structured answer template enforced via XML tags

```python
import anthropic
import re

client = anthropic.Anthropic()

SYSTEM = """You are a technical assistant. Structure every technical answer using these XML tags:

<answer>The direct answer to the question</answer>
<prerequisites>What must be true or installed for this to work (bullet points)</prerequisites>
<caveats>Important limitations, security notes, edge cases (bullet points)</caveats>
<example>A minimal working code example if applicable</example>

Use all four tags. If a section genuinely does not apply, write "None." inside the tag."""

def ask_structured(question: str) -> dict:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    text = r.content[0].text

    def extract(tag: str) -> str:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return m.group(1).strip() if m else ""

    return {
        "answer":        extract("answer"),
        "prerequisites": extract("prerequisites"),
        "caveats":       extract("caveats"),
        "example":       extract("example"),
    }

questions = [
    "How do I implement JWT authentication in Python?",
    "How do I safely run user-provided shell commands?",
    "What is 2 + 2?",   # genuinely simple — most tags will say "None."
]

for q in questions:
    print(f"Q: {q}")
    result = ask_structured(q)
    for section, content in result.items():
        if content and content != "None.":
            print(f"  [{section}]: {content[:120]}")
    print()
```

**Expected Token Savings:** XML tags enforce completeness structurally — the model must fill each section, preventing silent omission; downstream parsers can extract caveats programmatically to surface them in UI warning boxes separate from the main answer.
**Environment:** Documentation generators, knowledge bases, and agents that feed structured output to downstream systems; XML tagging enables both completeness enforcement and machine-parseable output.

---

### Option 6 — Completeness scorer: flag answers below a threshold for expansion

```python
import json
import anthropic

client = anthropic.Anthropic()

SCORER_SYSTEM = """Score the completeness of this technical answer on a 1-5 scale:
1 = dangerously incomplete (missing critical prerequisites or safety information)
2 = incomplete (missing important caveats or edge cases)
3 = adequate (covers the basics, minor gaps acceptable)
4 = thorough (covers prerequisites, key caveats, examples)
5 = comprehensive (covers all edge cases, tradeoffs, alternatives)

Return JSON: {"score": 1-5, "gaps": ["gap1", "gap2", ...]}"""

EXPAND_SYSTEM = "Expand this answer to address the identified gaps. Be thorough but concise."

COMPLETENESS_THRESHOLD = 3   # expand any answer scoring below this

def ask_with_completeness_check(question: str) -> str:
    # Initial answer
    r1 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": question}],
    )
    answer = r1.content[0].text.strip()

    # Score it
    score_prompt = f"Question: {question}\n\nAnswer: {answer}"
    r2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=96,
        system=SCORER_SYSTEM,
        messages=[{"role": "user", "content": score_prompt}],
    )
    raw = r2.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        scored = json.loads(raw)
    except json.JSONDecodeError:
        scored = {"score": 3, "gaps": []}

    score = scored.get("score", 3)
    gaps  = scored.get("gaps", [])
    print(f"  [score={score}/5] gaps={gaps[:3]}")

    if score < COMPLETENESS_THRESHOLD and gaps:
        expand_prompt = (
            f"Question: {question}\n\nCurrent answer: {answer}\n\n"
            f"Gaps to address:\n" + "\n".join(f"- {g}" for g in gaps)
        )
        r3 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=EXPAND_SYSTEM,
            messages=[{"role": "user", "content": expand_prompt}],
        )
        return r3.content[0].text.strip()

    return answer

questions = [
    "How do I store API keys in my Python app?",
    "How do I prevent SQL injection?",
    "What is the capital of France?",   # score will be 5 — no expansion needed
]

for q in questions:
    print(f"Q: {q}")
    answer = ask_with_completeness_check(q)
    print(f"A: {answer[:500]}\n")
```

**Expected Token Savings:** Completeness scorer only triggers expansion for genuinely incomplete answers; simple questions pass without expansion (no extra call), complex/security questions get expanded automatically — focuses token spend on the answers that actually need it.
**Environment:** Mixed-complexity agents where the cost of incompleteness varies; scoring lets the agent self-regulate verbosity based on measured completeness rather than question length heuristics.

---

## Comparison

| Option | Adds Tokens | Structured Output | Self-Correcting | Best For |
|---|---|---|---|---|
| 1. Completeness instruction | Yes (~200 avg) | No | No | All agents — baseline fix |
| 2. Depth selector | Adaptive | No | No | Mixed query agents |
| 3. Few-shot examples | Yes (fixed template) | Yes (by example) | No | Consistent format requirements |
| 4. Self-critique pass | Yes (+2 calls) | No | Yes | High-stakes/security answers |
| 5. XML tag structure | Yes (~50 overhead) | Yes (parseable) | No | Machine-processed answers |
| 6. Completeness scorer | Adaptive (+1-2 calls) | No | Yes | Automated quality gating |
