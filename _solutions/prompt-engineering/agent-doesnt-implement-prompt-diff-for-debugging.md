---
layout: solution
title: "Agent Doesn't Implement Prompt Diff for Debugging"
category: prompt-engineering
description: "Surface the exact diff between two prompt versions — showing which tokens changed, how system prompt edits affect output, and which variable substitutions caused behavior changes — so prompt regressions are debugged like code diffs."
tags: [prompt-engineering, debugging, diff, versioning, observability, python]
---

# Agent Doesn't Implement Prompt Diff for Debugging

When agent behavior changes after a prompt edit, developers cannot quickly identify what changed without manually comparing prompt strings. Prompt diff tooling surfaces the exact delta between versions — highlighting changed tokens, measuring semantic distance, and correlating prompt changes with output changes — turning prompt debugging from guesswork into a systematic process.

## Option 1: Character-Level Unified Diff

```python
import anthropic
import difflib

client = anthropic.Anthropic()

def prompt_diff(prompt_a: str, prompt_b: str, label_a: str = "v1", label_b: str = "v2") -> str:
    """Generate a unified diff between two prompt strings."""
    lines_a = prompt_a.splitlines(keepends=True)
    lines_b = prompt_b.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        lines_a, lines_b,
        fromfile=label_a, tofile=label_b,
        lineterm="",
    ))
    if not diff:
        return "(no changes)"
    return "\n".join(diff)

def diff_summary(prompt_a: str, prompt_b: str) -> dict:
    """Summarize the diff: added/removed lines, changed tokens."""
    lines_a = set(prompt_a.splitlines())
    lines_b = set(prompt_b.splitlines())
    added   = lines_b - lines_a
    removed = lines_a - lines_b
    words_a = set(prompt_a.split())
    words_b = set(prompt_b.split())
    return {
        "lines_added":   len(added),
        "lines_removed": len(removed),
        "words_added":   len(words_b - words_a),
        "words_removed": len(words_a - words_b),
        "char_delta":    len(prompt_b) - len(prompt_a),
        "similarity":    difflib.SequenceMatcher(None, prompt_a, prompt_b).ratio(),
    }

prompt_v1 = """You are a helpful assistant. Answer questions clearly.
When asked about code, provide examples.
Be concise and accurate."""

prompt_v2 = """You are a precise technical assistant. Answer questions clearly and concisely.
When asked about code, provide working Python examples with explanations.
Be accurate. Avoid speculation."""

diff = prompt_diff(prompt_v1, prompt_v2)
summary = diff_summary(prompt_v1, prompt_v2)

print("Prompt diff:")
print(diff)
print(f"\nSummary: {summary}")

# Run both prompts and compare outputs
def run(system: str, question: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text

question = "What is asyncio?"
out_v1 = run(prompt_v1, question)
out_v2 = run(prompt_v2, question)
output_diff = diff_summary(out_v1, out_v2)
print(f"\nOutput diff: {output_diff}")
print(f"v1: {out_v1[:80]!r}")
print(f"v2: {out_v2[:80]!r}")

# Expected Token Savings: Diff reveals which prompt tokens drove behavior change; avoids re-running whole test suite
# Environment: difflib is stdlib; SequenceMatcher.ratio() gives 0-1 similarity score
```

## Option 2: Token-Level Diff with Highlighted Changes

```python
import anthropic
import difflib
import re

client = anthropic.Anthropic()

def tokenize(text: str) -> list[str]:
    """Split prompt into meaningful tokens: words, punctuation, whitespace groups."""
    return re.findall(r'\S+|\s+', text)

def token_diff(prompt_a: str, prompt_b: str) -> list[dict]:
    """Return token-level opcodes for granular diff."""
    tokens_a = tokenize(prompt_a)
    tokens_b = tokenize(prompt_b)
    matcher  = difflib.SequenceMatcher(None, tokens_a, tokens_b)
    changes  = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        changes.append({
            "op":      op,
            "removed": "".join(tokens_a[i1:i2]),
            "added":   "".join(tokens_b[j1:j2]),
        })
    return changes

def render_diff(changes: list[dict]) -> str:
    """Render diff as readable text with markers."""
    parts = []
    for c in changes:
        if c["op"] == "replace":
            parts.append(f"[-{c['removed'].strip()!r}] -> [+{c['added'].strip()!r}]")
        elif c["op"] == "delete":
            parts.append(f"[-{c['removed'].strip()!r}]")
        elif c["op"] == "insert":
            parts.append(f"[+{c['added'].strip()!r}]")
    return "\n".join(parts) if parts else "(identical)"

# Compare prompt variants
v1 = "You are a helpful assistant. Answer briefly."
v2 = "You are a precise and helpful assistant. Answer briefly and accurately."
v3 = "You are a concise technical assistant. Always answer in bullet points."

print("v1 → v2 token diff:")
changes_12 = token_diff(v1, v2)
print(render_diff(changes_12))
print(f"  {len(changes_12)} token change(s)")

print("\nv1 → v3 token diff:")
changes_13 = token_diff(v1, v3)
print(render_diff(changes_13))
print(f"  {len(changes_13)} token change(s)")

# Run all three and show output diffs
def run(system: str, q: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=120,
        system=system,
        messages=[{"role": "user", "content": q}],
    )
    return resp.content[0].text

q = "What is Python?"
outputs = {v: run(v, q) for v in [v1, v2, v3]}
print("\nOutput lengths:", {k[:20]: len(v) for k, v in outputs.items()})
for label, out in outputs.items():
    print(f"  [{label[:30]!r}]: {out[:60]!r}")

# Expected Token Savings: Token-level diff shows exactly which words changed; no full re-run needed to spot regressions
# Environment: re.findall tokenizer works for prose; extend with spacy for linguistic tokenization
```

## Option 3: Variable Substitution Diff — Template vs Rendered

```python
import anthropic
import re
import difflib

client = anthropic.Anthropic()

def render_template(template: str, variables: dict) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result

def find_unresolved(rendered: str) -> list[str]:
    return re.findall(r"\{\{(\w+)\}\}", rendered)

def template_diff(template: str, vars_a: dict, vars_b: dict) -> dict:
    rendered_a = render_template(template, vars_a)
    rendered_b = render_template(template, vars_b)
    changed_vars = {k: (vars_a.get(k), vars_b.get(k))
                    for k in set(vars_a) | set(vars_b)
                    if vars_a.get(k) != vars_b.get(k)}
    similarity = difflib.SequenceMatcher(None, rendered_a, rendered_b).ratio()
    return {
        "rendered_a":    rendered_a,
        "rendered_b":    rendered_b,
        "changed_vars":  changed_vars,
        "unresolved_a":  find_unresolved(rendered_a),
        "unresolved_b":  find_unresolved(rendered_b),
        "similarity":    round(similarity, 3),
        "char_delta":    len(rendered_b) - len(rendered_a),
    }

# Template with variable slots
SYSTEM_TEMPLATE = """You are a {{role}} assistant specialized in {{domain}}.
Respond in {{language}}. Target audience: {{audience}}.
Maximum response length: {{max_words}} words."""

vars_v1 = {"role": "helpful", "domain": "Python", "language": "English",
            "audience": "beginners", "max_words": "100"}
vars_v2 = {"role": "expert", "domain": "Python asyncio", "language": "English",
            "audience": "senior engineers", "max_words": "200"}

diff = template_diff(SYSTEM_TEMPLATE, vars_v1, vars_v2)
print("Changed variables:")
for var, (old, new) in diff["changed_vars"].items():
    print(f"  {var}: {old!r} -> {new!r}")
print(f"Similarity: {diff['similarity']:.0%}")
print(f"Char delta: {diff['char_delta']:+d}")

# Run both rendered prompts
def run(system: str, q: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=system,
        messages=[{"role": "user", "content": q}],
    )
    return resp.content[0].text

q = "Explain event loops."
out_v1 = run(diff["rendered_a"], q)
out_v2 = run(diff["rendered_b"], q)
output_sim = difflib.SequenceMatcher(None, out_v1, out_v2).ratio()
print(f"\nPrompt similarity: {diff['similarity']:.0%}")
print(f"Output similarity: {output_sim:.0%}")
print(f"v1 output: {out_v1[:80]!r}")
print(f"v2 output: {out_v2[:80]!r}")

# Expected Token Savings: Variable diff isolates which slot drove behavior change; no full prompt re-diff needed
# Environment: Jinja2 or string.Template for production templates; find_unresolved catches missing vars before API call
```

## Option 4: SQLite Prompt Version Store with Diff API

```python
import anthropic
import sqlite3
import difflib
import json
import time

client = anthropic.Anthropic()
DB = "prompt_versions.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, version TEXT, content TEXT,
            created_ts REAL, author TEXT, notes TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS prompt_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_name TEXT, version TEXT, question TEXT,
            response TEXT, tokens INTEGER, ts REAL
        )
    """)
    con.commit(); con.close()

def save_version(name: str, version: str, content: str,
                 author: str = "auto", notes: str = "") -> int:
    init_db()
    con = sqlite3.connect(DB)
    cur = con.execute(
        "INSERT INTO prompt_versions (name, version, content, created_ts, author, notes) VALUES (?,?,?,?,?,?)",
        (name, version, content, time.time(), author, notes),
    )
    con.commit(); con.close()
    return cur.lastrowid

def get_version(name: str, version: str) -> str | None:
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT content FROM prompt_versions WHERE name=? AND version=? ORDER BY id DESC LIMIT 1",
        (name, version),
    ).fetchone()
    con.close()
    return row[0] if row else None

def diff_versions(name: str, v_from: str, v_to: str) -> dict:
    content_a = get_version(name, v_from) or ""
    content_b = get_version(name, v_to)   or ""
    lines_a   = content_a.splitlines(keepends=True)
    lines_b   = content_b.splitlines(keepends=True)
    unified   = "".join(difflib.unified_diff(lines_a, lines_b,
                                              fromfile=f"{name}@{v_from}",
                                              tofile=f"{name}@{v_to}",
                                              lineterm=""))
    sim = difflib.SequenceMatcher(None, content_a, content_b).ratio()
    return {
        "diff":       unified,
        "similarity": round(sim, 3),
        "added":      sum(1 for l in unified.splitlines() if l.startswith("+")),
        "removed":    sum(1 for l in unified.splitlines() if l.startswith("-")),
    }

def run_and_log(name: str, version: str, question: str) -> str:
    content = get_version(name, version)
    if not content:
        raise KeyError(f"Prompt {name}@{version} not found")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=content,
        messages=[{"role": "user", "content": question}],
    )
    text = resp.content[0].text
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO prompt_runs (prompt_name, version, question, response, tokens, ts) VALUES (?,?,?,?,?,?)",
                (name, version, question, text, resp.usage.input_tokens + resp.usage.output_tokens, time.time()))
    con.commit(); con.close()
    return text

# Register versions
save_version("assistant", "1.0", "You are a helpful assistant. Be concise.", notes="initial")
save_version("assistant", "1.1", "You are a precise assistant. Be concise and accurate.", notes="added 'precise'")
save_version("assistant", "2.0", "You are an expert technical assistant.\nProvide accurate, concise answers with code examples when relevant.", notes="major revision")

diff_10_11 = diff_versions("assistant", "1.0", "1.1")
diff_11_20 = diff_versions("assistant", "1.1", "2.0")

print(f"1.0→1.1: {diff_10_11['added']}+ {diff_10_11['removed']}- similarity={diff_10_11['similarity']:.0%}")
print(f"1.1→2.0: {diff_11_20['added']}+ {diff_11_20['removed']}- similarity={diff_11_20['similarity']:.0%}")
print(f"\nDiff 1.0→1.1:\n{diff_10_11['diff']}")

q = "What is asyncio?"
for v in ["1.0", "1.1", "2.0"]:
    out = run_and_log("assistant", v, q)
    print(f"v{v}: {out[:70]!r}")

# Expected Token Savings: SQLite version store enables bisect debugging; find exact version where regression appeared
# Environment: extend with git integration (store version=git SHA); query prompt_runs for A/B latency comparison
```

## Option 5: Semantic Diff — Meaning Change Beyond Surface Edit

```python
import anthropic
import difflib
import re

client = anthropic.Anthropic()

def semantic_diff(prompt_a: str, prompt_b: str) -> dict:
    """Assess semantic change by testing both prompts on probe questions."""
    probe_questions = [
        "What should I do if I'm unsure?",
        "How long should your response be?",
        "What format should you use?",
    ]
    surface_sim = difflib.SequenceMatcher(None, prompt_a, prompt_b).ratio()
    responses_a = []
    responses_b = []
    for q in probe_questions:
        for prompt, container in [(prompt_a, responses_a), (prompt_b, responses_b)]:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system=prompt,
                messages=[{"role": "user", "content": q}],
            )
            container.append(resp.content[0].text)

    # Compare behavioral outputs
    behavior_sims = [
        difflib.SequenceMatcher(None, a, b).ratio()
        for a, b in zip(responses_a, responses_b)
    ]
    avg_behavior_sim = sum(behavior_sims) / len(behavior_sims)

    return {
        "surface_similarity":  round(surface_sim, 3),
        "behavior_similarity": round(avg_behavior_sim, 3),
        "semantic_drift": surface_sim > 0.9 and avg_behavior_sim < 0.6,
        "probes": [
            {"q": q, "a": a[:60], "b": b[:60], "sim": round(s, 2)}
            for q, a, b, s in zip(probe_questions, responses_a, responses_b, behavior_sims)
        ],
    }

prompt_a = "You are a helpful assistant. Answer questions concisely."
prompt_b = "You are a helpful assistant. Answer questions concisely."  # identical
prompt_c = "You are a terse assistant. Respond only with bullet points. No prose."

print("=== Identical prompts ===")
diff_ab = semantic_diff(prompt_a, prompt_b)
print(f"Surface sim: {diff_ab['surface_similarity']:.0%} | Behavior sim: {diff_ab['behavior_similarity']:.0%}")
print(f"Semantic drift detected: {diff_ab['semantic_drift']}")

print("\n=== Different behavior prompts ===")
diff_ac = semantic_diff(prompt_a, prompt_c)
print(f"Surface sim: {diff_ac['surface_similarity']:.0%} | Behavior sim: {diff_ac['behavior_similarity']:.0%}")
print(f"Semantic drift detected: {diff_ac['semantic_drift']}")
for probe in diff_ac["probes"]:
    print(f"  Q: {probe['q'][:40]!r}")
    print(f"    A: {probe['a']!r}")
    print(f"    B: {probe['b']!r}")
    print(f"    Similarity: {probe['sim']:.0%}")

# Expected Token Savings: Semantic diff reveals silent behavior changes from small surface edits; 6 probe calls total
# Environment: probe_questions tunable to your domain; semantic_drift=True triggers review flag in CI
```

## Option 6: Prompt Diff CI Gate — Block Deploys on Behavior Regression

```python
import anthropic
import difflib
import sqlite3
import time
import sys

client = anthropic.Anthropic()
DB = "prompt_ci.db"

GOLDEN_TESTS = [
    {
        "question":        "What is Python?",
        "expected_topics": ["programming", "language", "interpreted"],
        "min_length":      50,
        "max_length":      300,
    },
    {
        "question":        "What is asyncio?",
        "expected_topics": ["async", "concurrent", "event loop"],
        "min_length":      50,
        "max_length":      300,
    },
]

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ci_runs (
            ts REAL, version TEXT, prompt_hash TEXT,
            test_question TEXT, passed INTEGER, reason TEXT
        )
    """)
    con.commit(); con.close()

def run_test(system: str, test: dict, version: str) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": test["question"]}],
    )
    output = resp.content[0].text
    failures = []
    if len(output) < test["min_length"]:
        failures.append(f"Too short: {len(output)} < {test['min_length']}")
    if len(output) > test["max_length"]:
        failures.append(f"Too long: {len(output)} > {test['max_length']}")
    for topic in test["expected_topics"]:
        if topic.lower() not in output.lower():
            failures.append(f"Missing topic: {topic!r}")
    passed = len(failures) == 0
    reason = "; ".join(failures) if failures else "ok"
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO ci_runs VALUES (?,?,?,?,?,?)",
                (time.time(), version, str(hash(system))[:8], test["question"], int(passed), reason))
    con.commit(); con.close()
    return {"passed": passed, "reason": reason, "output_len": len(output)}

def ci_gate(old_prompt: str, new_prompt: str,
            old_version: str = "current", new_version: str = "candidate") -> dict:
    init_db()
    print(f"Prompt diff ({old_version} → {new_version}):")
    diff_lines = list(difflib.unified_diff(
        old_prompt.splitlines(keepends=True),
        new_prompt.splitlines(keepends=True),
        fromfile=old_version, tofile=new_version, lineterm="",
    ))
    for line in diff_lines[:10]:
        print(f"  {line.rstrip()}")
    similarity = difflib.SequenceMatcher(None, old_prompt, new_prompt).ratio()
    print(f"  Similarity: {similarity:.0%}")

    print(f"\nRunning {len(GOLDEN_TESTS)} tests against {new_version}:")
    results = [run_test(new_prompt, t, new_version) for t in GOLDEN_TESTS]
    passed  = sum(1 for r in results if r["passed"])
    for test, result in zip(GOLDEN_TESTS, results):
        marker = "✓" if result["passed"] else "✗"
        print(f"  {marker} {test['question'][:40]!r}: {result['reason']}")

    gate_passed = passed == len(GOLDEN_TESTS)
    print(f"\nCI gate: {'PASS' if gate_passed else 'FAIL'} ({passed}/{len(GOLDEN_TESTS)})")
    return {"passed": gate_passed, "tests_passed": passed, "total": len(GOLDEN_TESTS)}

old = "You are a helpful assistant. Answer questions about Python clearly."
new = "You are a technical Python expert. Answer concisely with examples."

result = ci_gate(old, new)
if not result["passed"]:
    print("Deploy blocked.")
    # sys.exit(1)  # uncomment in CI
else:
    print("Deploy approved.")

# Expected Token Savings: CI gate catches regressions before deploy; golden tests are cheap Haiku calls
# Environment: integrate ci_gate() into GitHub Actions; adjust GOLDEN_TESTS to your domain coverage requirements
```

## Comparison

| Option | Diff Granularity | Behavior Testing | SQLite Storage | CI Integration |
|--------|-----------------|-----------------|---------------|---------------|
| 1 — Unified Diff | Line-level | Side-by-side output | No | No |
| 2 — Token Diff | Token-level | Side-by-side output | No | No |
| 3 — Variable Diff | Template slot | Side-by-side output | No | No |
| 4 — Version Store | Line-level | Historical runs | Yes | No |
| 5 — Semantic Diff | Behavior probes | 3 probe questions | No | No |
| 6 — CI Gate | Line-level | Golden test suite | Yes | Yes |
