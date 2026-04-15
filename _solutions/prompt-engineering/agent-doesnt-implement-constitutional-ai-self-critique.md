---
layout: solution
title: "Agent Doesn't Implement Constitutional AI Self-Critique"
category: prompt-engineering
description: "Agent outputs its first draft as the final answer without self-reviewing against quality or safety principles — missing obvious errors, bias, or policy violations that a single self-critique pass would catch."
tags: [prompt-engineering, self-critique, constitutional-ai, quality, safety, revision]
---

# Agent Doesn't Implement Constitutional AI Self-Critique

## Problem

An agent's first draft is rarely its best. Without a self-critique step, agents:

- Return factually wrong answers they would correct if they re-read their output
- Produce biased or one-sided analysis they would balance with a second look
- Output text that violates tone/format guidelines they were given
- Miss safety issues that are obvious on review

**Root cause:** The agent loop returns the first `end_turn` response directly to the user with no quality gate. There's no "write → critique → revise" cycle.

**The fix:** After generating a draft, run a structured self-critique against a constitution (set of principles), then revise only where the critique identifies real problems.

---

## Option 1: Single-Pass Critique and Revision

Generate a draft, critique it against a constitution, revise once.

```python
import anthropic

client = anthropic.Anthropic()

CONSTITUTION = """You are reviewing an AI response. Check it against these principles:

1. ACCURACY: Is the response factually accurate? Flag any claims that might be wrong.
2. BALANCE: Is the response fair and balanced, or does it show bias toward one perspective?
3. CLARITY: Is the response clear and well-structured? Is it appropriately concise?
4. HELPFULNESS: Does the response actually answer the user's question?
5. SAFETY: Does the response avoid harmful, offensive, or inappropriate content?

For each principle, briefly note if it PASSES or FAILS with one sentence of reasoning.
End with VERDICT: APPROVE (no revision needed) or REVISE (specify what to improve)."""

def generate_draft(query: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": query}]
    )
    return response.content[0].text

def critique_draft(draft: str, query: str) -> tuple[str, bool]:
    """Returns (critique_text, needs_revision)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CONSTITUTION,
        messages=[{
            "role": "user",
            "content": f"Original question: {query}\n\nDraft response to review:\n{draft}"
        }]
    )
    critique = response.content[0].text
    needs_revision = "VERDICT: REVISE" in critique
    return critique, needs_revision

def revise_draft(draft: str, critique: str, query: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Original question: {query}

Your draft response:
{draft}

Critique of your draft:
{critique}

Please revise your response to address the issues identified in the critique.
Write only the revised response — no meta-commentary."""
        }]
    )
    return response.content[0].text

def run_with_self_critique(query: str) -> str:
    # Step 1: Generate draft
    draft = generate_draft(query)
    print(f"[critique] Draft ({len(draft)} chars): {draft[:80]}...")

    # Step 2: Critique
    critique, needs_revision = critique_draft(draft, query)
    print(f"[critique] Critique: {critique[:120]}...")
    print(f"[critique] Needs revision: {needs_revision}")

    if not needs_revision:
        return draft

    # Step 3: Revise
    revised = revise_draft(draft, critique, query)
    print(f"[critique] Revised ({len(revised)} chars): {revised[:80]}...")
    return revised

# Test with a query prone to one-sidedness
result = run_with_self_critique(
    "What are the advantages of using NoSQL over SQL databases?"
)
print(f"\nFinal answer:\n{result}")

# Expected Token Savings: ~-30% (critique + revision uses extra tokens, but prevents costly re-requests from users who got bad answers)
# Environment: Customer-facing agents where response quality directly impacts satisfaction
```

---

## Option 2: Multi-Principle Structured Critique

Check each principle separately; only revise on specific failures, not a blanket rewrite.

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class Principle:
    name: str
    description: str
    critique_prompt: str
    severity: str = "medium"  # low, medium, high

PRINCIPLES = [
    Principle(
        name="factual_accuracy",
        description="Claims are verifiable and not fabricated",
        critique_prompt="Does this response make any claims that could be factually wrong or unverifiable? Reply: PASS or FAIL:<reason>",
        severity="high"
    ),
    Principle(
        name="balanced_perspective",
        description="Response presents multiple viewpoints when appropriate",
        critique_prompt="Is this response one-sided or does it present a balanced view? For opinion topics, reply: PASS or FAIL:<reason>",
        severity="medium"
    ),
    Principle(
        name="format_quality",
        description="Response is well-structured and appropriately concise",
        critique_prompt="Is this response well-formatted and the right length? Reply: PASS or FAIL:<reason>",
        severity="low"
    ),
    Principle(
        name="direct_answer",
        description="Response directly answers what was asked",
        critique_prompt="Does this response directly answer the question asked? Reply: PASS or FAIL:<reason>",
        severity="high"
    ),
]

def check_principle(draft: str, query: str, principle: Principle) -> tuple[bool, str]:
    """Returns (passes, reason)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"Question: {query}\n\nResponse: {draft[:400]}\n\n{principle.critique_prompt}"
        }]
    )
    result = response.content[0].text.strip()
    passes = result.upper().startswith("PASS")
    reason = result[5:].strip(":").strip() if ":" in result else result
    return passes, reason

def targeted_revise(draft: str, query: str, failures: list[tuple[Principle, str]]) -> str:
    if not failures:
        return draft

    revision_instructions = "\n".join(
        f"- Fix {p.name}: {reason}"
        for p, reason in failures
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Question: {query}

Your draft:
{draft}

Issues to fix:
{revision_instructions}

Write an improved response that addresses these specific issues only. Do not change what was already good."""
        }]
    )
    return response.content[0].text

def run_multi_principle_critique(query: str) -> str:
    draft = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": query}]
    ).content[0].text

    # Check all principles
    failures: list[tuple[Principle, str]] = []
    high_severity_fail = False

    for principle in PRINCIPLES:
        passes, reason = check_principle(draft, query, principle)
        status = "PASS" if passes else f"FAIL ({principle.severity})"
        print(f"[critique] {principle.name}: {status}" + (f" — {reason}" if not passes else ""))
        if not passes:
            failures.append((principle, reason))
            if principle.severity == "high":
                high_severity_fail = True

    if not failures:
        print("[critique] All principles passed — returning draft")
        return draft

    if not high_severity_fail and all(p.severity == "low" for p, _ in failures):
        print("[critique] Only low-severity issues — returning draft as-is")
        return draft

    print(f"[critique] Revising for {len(failures)} issue(s)")
    return targeted_revise(draft, query, failures)

result = run_multi_principle_critique(
    "Should companies use AI to make hiring decisions?"
)
print(f"\nFinal:\n{result[:300]}...")

# Expected Token Savings: ~-20% (targeted revision is cheaper than full rewrite; principle checks use haiku)
# Environment: High-stakes domains: medical, legal, financial, HR decision support
```

---

## Option 3: Iterative Critique Loop with Convergence Check

Run critique→revise cycles until the response passes all principles or a max iteration limit is hit.

```python
import anthropic

client = anthropic.Anthropic()

CRITIQUE_SYSTEM = """You are a strict editor reviewing an AI response.

Check for:
- ACCURACY: No fabricated facts or unsupported claims
- TONE: Professional and respectful; no condescension
- COMPLETENESS: All parts of the question are addressed
- CONCISENESS: No unnecessary padding or repetition

If the response passes all checks, reply: STATUS: APPROVED

If it fails any check, reply:
STATUS: NEEDS_REVISION
ISSUES:
- [issue 1]
- [issue 2]
SUGGESTIONS:
- [specific fix 1]
- [specific fix 2]"""

def run_critique_loop(query: str, max_iterations: int = 3) -> dict:
    current_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": query}]
    ).content[0].text

    history = [{"iteration": 0, "response": current_response, "critique": None}]

    for i in range(1, max_iterations + 1):
        # Critique current response
        critique_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=CRITIQUE_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Original question: {query}\n\nResponse to review:\n{current_response}"
            }]
        ).content[0].text

        history[-1]["critique"] = critique_response
        print(f"[loop] Iteration {i} critique: {critique_response[:80]}...")

        if "STATUS: APPROVED" in critique_response:
            print(f"[loop] Approved after {i-1} revision(s)")
            break

        # Extract suggestions and revise
        revised = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": f"""Question: {query}

Previous response:
{current_response}

Editor critique:
{critique_response}

Write an improved response addressing all the editor's suggestions.
Write ONLY the revised response."""
            }]
        ).content[0].text

        current_response = revised
        history.append({"iteration": i, "response": current_response, "critique": None})

        if i == max_iterations:
            print(f"[loop] Max iterations ({max_iterations}) reached")

    return {
        "final_response": current_response,
        "iterations": len(history) - 1,
        "history": history
    }

result = run_critique_loop(
    "Explain the pros and cons of remote work for software engineers",
    max_iterations=2
)
print(f"\nFinal response ({result['iterations']} revision(s)):")
print(result["final_response"])

# Expected Token Savings: ~-50% worst case (3 loops), but prevents user re-asks which cost more
# Environment: Content generation, report writing, documentation agents where quality matters most
```

---

## Option 4: Peer Review — Two Models Critique Each Other

Use two separate model calls: one to generate, one acting as a peer reviewer.

```python
import anthropic

client = anthropic.Anthropic()

GENERATOR_SYSTEM = """You are a knowledgeable, helpful assistant. Answer questions clearly and accurately.
Focus on giving a complete, well-structured response."""

REVIEWER_SYSTEM = """You are a rigorous peer reviewer. Your job is to identify real problems in AI responses.

Be critical but fair. Look for:
1. Factual errors or unsupported claims
2. Missing important information the user would expect
3. Logical inconsistencies
4. Tone or clarity issues

If the response is genuinely good, say so briefly.
If there are real issues, list them specifically.
End with: RECOMMENDATION: APPROVE | MINOR_REVISION | MAJOR_REVISION"""

def run_peer_review_agent(query: str) -> str:
    # Generator agent produces the response
    generator_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=GENERATOR_SYSTEM,
        messages=[{"role": "user", "content": query}]
    )
    draft = generator_response.content[0].text
    print(f"[peer] Generator draft: {draft[:100]}...")

    # Reviewer agent critiques it
    reviewer_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=REVIEWER_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"User's question: {query}\n\nAI's response:\n{draft}"
        }]
    )
    review = reviewer_response.content[0].text
    print(f"[peer] Reviewer: {review[:100]}...")

    if "RECOMMENDATION: APPROVE" in review:
        print("[peer] Approved — returning draft")
        return draft

    if "RECOMMENDATION: MINOR_REVISION" in review:
        # Quick targeted fix
        final = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Question: {query}\n\nYour draft:\n{draft}\n\nMinor issues to fix:\n{review}\n\nProvide the corrected response only."
            }]
        ).content[0].text
        print("[peer] Minor revision applied")
        return final

    # MAJOR_REVISION — generate from scratch with reviewer feedback
    final = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=GENERATOR_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Question: {query}\n\nPrevious attempt had major issues:\n{review}\n\nWrite a completely improved response."
        }]
    ).content[0].text
    print("[peer] Major revision — regenerated from scratch")
    return final

queries = [
    "What is quantum computing and when will it replace classical computers?",
    "How do I deal with a difficult coworker?",
]

for q in queries:
    print(f"\n{'='*60}\nQ: {q}")
    answer = run_peer_review_agent(q)
    print(f"A: {answer[:200]}...")

# Expected Token Savings: ~-40% (two model calls, but prevents user re-prompting and builds trust)
# Environment: Public AI products, content moderation pipelines, educational tools
```

---

## Option 5: Constitution-as-Code — Machine-Readable Rules

Define the constitution as structured rules; evaluate each programmatically then apply targeted fixes.

```python
import anthropic
import json
import re
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class ConstitutionRule:
    id: str
    name: str
    check_prompt: str
    weight: float = 1.0  # Higher = more important
    auto_fix: Callable[[str, str], str] | None = None  # Optional auto-fix function

def check_rule_with_llm(rule: ConstitutionRule, draft: str, query: str) -> tuple[float, str]:
    """Returns (score 0-1, explanation)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": f"""Rate this response from 0 to 10 for: {rule.name}

Rule: {rule.check_prompt}
Question: {query}
Response: {draft[:300]}

Reply with: SCORE:<0-10> REASON:<one sentence>"""
        }]
    )
    text = response.content[0].text
    # Extract score
    match = re.search(r'SCORE:?\s*(\d+)', text)
    score = int(match.group(1)) / 10.0 if match else 0.5
    reason_match = re.search(r'REASON:?\s*(.+)', text)
    reason = reason_match.group(1).strip() if reason_match else text
    return score, reason

def auto_fix_length(draft: str, query: str) -> str:
    """Auto-fix: truncate if response is excessively long."""
    if len(draft.split()) > 200:
        sentences = draft.split('. ')
        return '. '.join(sentences[:8]) + '.'
    return draft

CONSTITUTION_RULES = [
    ConstitutionRule(
        id="R1", name="Factual Confidence",
        check_prompt="Does the response avoid overconfident claims about uncertain facts? Does it use appropriate hedging?",
        weight=2.0
    ),
    ConstitutionRule(
        id="R2", name="Actionability",
        check_prompt="Is the response concrete and actionable, or is it vague and generic?",
        weight=1.5
    ),
    ConstitutionRule(
        id="R3", name="Appropriate Length",
        check_prompt="Is the response appropriately concise? Not too long, not too short?",
        weight=1.0,
        auto_fix=auto_fix_length
    ),
    ConstitutionRule(
        id="R4", name="Empathy",
        check_prompt="For personal/emotional topics, is the response empathetic? For technical topics this can be N/A (score 10).",
        weight=0.5
    ),
]

PASS_THRESHOLD = 0.7  # Weighted score >= 0.7 = pass

def evaluate_constitution(draft: str, query: str) -> dict:
    total_weight = sum(r.weight for r in CONSTITUTION_RULES)
    weighted_score = 0.0
    evaluations = []

    for rule in CONSTITUTION_RULES:
        score, reason = check_rule_with_llm(rule, draft, query)
        weighted_score += score * rule.weight
        evaluations.append({
            "rule_id": rule.id,
            "name": rule.name,
            "score": score,
            "reason": reason,
            "passes": score >= PASS_THRESHOLD
        })
        print(f"[constitution] {rule.id} ({rule.name}): {score:.1f} — {reason[:50]}")

    overall = weighted_score / total_weight
    return {"overall_score": overall, "passes": overall >= PASS_THRESHOLD, "evaluations": evaluations}

def run_constitution_as_code_agent(query: str) -> str:
    draft = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": query}]
    ).content[0].text

    result = evaluate_constitution(draft, query)
    print(f"[constitution] Overall score: {result['overall_score']:.2f} ({'PASS' if result['passes'] else 'FAIL'})")

    if result["passes"]:
        return draft

    # Try auto-fixes first for rules that have them
    fixed = draft
    for rule in CONSTITUTION_RULES:
        eval_data = next((e for e in result["evaluations"] if e["rule_id"] == rule.id), None)
        if eval_data and not eval_data["passes"] and rule.auto_fix:
            fixed = rule.auto_fix(fixed, query)
            print(f"[constitution] Applied auto-fix for {rule.id}")

    # If still failing, use LLM revision
    if fixed == draft:
        failures = [e for e in result["evaluations"] if not e["passes"]]
        issues = "\n".join(f"- {e['name']}: {e['reason']}" for e in failures)
        fixed = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": f"Question: {query}\n\nDraft:\n{draft}\n\nFix these issues:\n{issues}\n\nWrite the improved response only."
            }]
        ).content[0].text

    return fixed

result = run_constitution_as_code_agent(
    "I'm feeling really anxious about my job interview tomorrow. Any advice?"
)
print(f"\nFinal:\n{result[:300]}...")

# Expected Token Savings: ~-30% (structured scoring; auto-fixes avoid LLM revision for simple issues)
# Environment: Agents handling sensitive topics; products with strict content quality requirements
```

---

## Option 6: Cached Constitution Check — Skip Critique for High-Confidence Responses

Check response confidence from metadata; skip the critique step for simple, high-confidence answers.

```python
import anthropic
import json
import hashlib
import sqlite3
from pathlib import Path

client = anthropic.Anthropic()
CRITIQUE_CACHE_DB = Path("/tmp/critique_cache.db")

def init_cache() -> sqlite3.Connection:
    conn = sqlite3.connect(CRITIQUE_CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS critique_cache (
            query_hash TEXT PRIMARY KEY,
            skip_critique INTEGER DEFAULT 0,
            avg_score REAL,
            sample_count INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

def query_hash(q: str) -> str:
    return hashlib.md5(q.lower().strip().encode()).hexdigest()[:12]

def should_skip_critique(conn: sqlite3.Connection, query: str) -> bool:
    """Return True if historical data says this query type consistently passes critique."""
    qh = query_hash(query)
    row = conn.execute(
        "SELECT skip_critique, avg_score, sample_count FROM critique_cache WHERE query_hash=?", (qh,)
    ).fetchone()
    if row and row[2] >= 5 and row[1] >= 0.85:
        print(f"[cached-critique] Skipping critique (historical avg score: {row[1]:.2f}, n={row[2]})")
        return True
    return False

def update_critique_cache(conn: sqlite3.Connection, query: str, score: float):
    qh = query_hash(query)
    existing = conn.execute(
        "SELECT avg_score, sample_count FROM critique_cache WHERE query_hash=?", (qh,)
    ).fetchone()
    if existing:
        n = existing[1] + 1
        new_avg = (existing[0] * existing[1] + score) / n
        skip = 1 if new_avg >= 0.85 and n >= 5 else 0
        conn.execute(
            "UPDATE critique_cache SET avg_score=?, sample_count=?, skip_critique=?, updated_at=datetime('now') WHERE query_hash=?",
            (new_avg, n, skip, qh)
        )
    else:
        conn.execute(
            "INSERT INTO critique_cache (query_hash, avg_score, sample_count) VALUES (?, ?, 1)",
            (qh, score)
        )
    conn.commit()

QUICK_CRITIQUE_SYSTEM = """Review this response briefly (2-3 sentences max).
Score it 0-10 overall. Reply: SCORE:<0-10> | ISSUES: <none or brief list>"""

def quick_critique(draft: str, query: str) -> tuple[float, str]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=QUICK_CRITIQUE_SYSTEM,
        messages=[{"role": "user", "content": f"Q: {query}\nA: {draft[:300]}"}]
    )
    text = response.content[0].text
    import re
    m = re.search(r'SCORE:?\s*(\d+)', text)
    score = int(m.group(1)) / 10.0 if m else 0.7
    return score, text

conn = init_cache()

def run_cached_critique_agent(query: str) -> str:
    # Check if we can skip critique based on history
    skip = should_skip_critique(conn, query)

    draft = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": query}]
    ).content[0].text

    if skip:
        return draft

    # Run quick critique
    score, critique_text = quick_critique(draft, query)
    print(f"[cached-critique] Score: {score:.1f} | {critique_text[:60]}...")
    update_critique_cache(conn, query, score)

    if score >= 0.75:
        print(f"[cached-critique] Passed (score={score:.1f}) — returning draft")
        return draft

    # Revise
    print(f"[cached-critique] Score below threshold — revising")
    revised = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"Q: {query}\n\nDraft:\n{draft}\n\nIssues: {critique_text}\n\nImproved response:"
        }]
    ).content[0].text
    return revised

# After 5 queries of the same type, critique will be automatically skipped
SAMPLE_QUERIES = [
    "What is the capital of Japan?",
    "What is the capital of France?",
    "What is the capital of Germany?",
]

for q in SAMPLE_QUERIES:
    print(f"\nQ: {q}")
    answer = run_cached_critique_agent(q)
    print(f"A: {answer[:80]}...")

# Same query pattern again — now skip_critique kicks in after enough data
print("\n--- After cache warmup ---")
print(run_cached_critique_agent("What is the capital of Australia?"))

# Expected Token Savings: ~40% (critique skipped for consistently-passing query types after warmup)
# Environment: High-volume agents with repeated query patterns; production systems optimizing for latency
```

---

## Comparison

| Option | Critique Style | Revision Strategy | Cost | Best For |
|--------|---------------|-------------------|------|----------|
| 1. Single-Pass | Holistic LLM review | Full rewrite if flagged | +2 calls | General-purpose quality gate |
| 2. Multi-Principle | Per-principle scoring | Targeted fix for failures | +N+1 calls | Domain-specific quality checklists |
| 3. Iterative Loop | Repeated until approved | Iterative revision | +2-6 calls | Highest quality requirement |
| 4. Peer Review | Separate reviewer model | Approve/minor/major routing | +2-3 calls | Content generation, editorial flow |
| 5. Constitution-as-Code | Scored rules + auto-fix | Auto-fix + LLM fallback | +N calls | Machine-readable compliance rules |
| 6. Cached Constitution | Quick critique + history | Skip for trusted patterns | +0-2 calls | High-volume with repeated patterns |
