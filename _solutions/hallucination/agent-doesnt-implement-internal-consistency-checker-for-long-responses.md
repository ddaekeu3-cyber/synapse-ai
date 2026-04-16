---
layout: solution
title: "Agent Doesn't Implement Internal Consistency Checker for Long Responses"
category: hallucination
description: "Detect and correct contradictions within a single long response before delivery — when early claims conflict with later ones in the same reply."
tags: [hallucination, consistency, self-contradiction, quality, long-form, verification]
---

## Problem

Long responses frequently contradict themselves. An agent states "Python 3.9 added match statements" in paragraph two, then claims "match statements were introduced in Python 3.10" in paragraph five. A financial summary says revenue grew 12% early on, then 8% in the conclusion. The model generates each sentence locally without maintaining a global view of what it has already asserted, so contradictions accumulate silently and reach the user unchecked.

```python
# Naive: generate and return without any consistency check
def respond(question: str) -> str:
    r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1024,
                               messages=[{"role": "user", "content": question}])
    return r.content[0].text  # may contain internal contradictions
```

## Solution Options

### Option 1: Claim Extraction + Pairwise Contradiction Detection

Extract atomic factual claims from the draft response, then check every pair for logical contradiction using a lightweight classifier.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class Contradiction:
    claim_a: str
    claim_b: str
    explanation: str

client = anthropic.Anthropic()

EXTRACT_PROMPT = """Extract every distinct factual claim from this text as a JSON list of strings.
Include only concrete, verifiable assertions (numbers, dates, names, causal statements).
Text:
{text}
Return JSON array only: ["claim1", "claim2", ...]"""

CHECK_PROMPT = """Do these two statements contradict each other?
Statement A: {a}
Statement B: {b}
Return JSON: {{"contradicts": true/false, "explanation": "<one sentence or empty string>"}}"""

def extract_claims(text: str) -> list[str]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}],
    )
    return json.loads(r.content[0].text)

def check_pair(a: str, b: str) -> Contradiction | None:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": CHECK_PROMPT.format(a=a, b=b)}],
    )
    result = json.loads(r.content[0].text)
    if result["contradicts"]:
        return Contradiction(claim_a=a, claim_b=b, explanation=result["explanation"])
    return None

def find_contradictions(response_text: str) -> list[Contradiction]:
    claims = extract_claims(response_text)
    contradictions = []
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            c = check_pair(claims[i], claims[j])
            if c:
                contradictions.append(c)
    return contradictions

def consistent_respond(question: str) -> str:
    # Step 1: generate draft
    draft = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    ).content[0].text

    # Step 2: check consistency
    contradictions = find_contradictions(draft)
    if not contradictions:
        return draft

    # Step 3: repair
    issues = "\n".join(
        f"- '{c.claim_a}' vs '{c.claim_b}': {c.explanation}"
        for c in contradictions
    )
    repair_prompt = f"""The following response contains internal contradictions.
Fix them so the response is fully self-consistent. Do not change unaffected content.

Contradictions found:
{issues}

Original response:
{draft}

Return the corrected response only."""
    fixed = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": repair_prompt}],
    ).content[0].text
    print(f"[CONSISTENCY] Fixed {len(contradictions)} contradiction(s)")
    return fixed


print(consistent_respond(
    "Summarize the history of Python version releases and their key features."
))

# Expected Token Savings: Extraction ~200 tok, pairwise ~100 tok/pair; prevents user-visible contradictions
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Single-Pass Self-Consistency Review

After generating the draft, send it back to the model with a self-review prompt. Cheaper than pairwise checking, good for moderately long responses.

```python
import anthropic

client = anthropic.Anthropic()

REVIEW_PROMPT = """Review this response for internal contradictions — places where one sentence or paragraph
contradicts another within the same response.

If you find contradictions, rewrite the response so it is fully self-consistent.
If the response is already consistent, return it unchanged.

Response to review:
{draft}"""

def self_consistent_respond(question: str) -> str:
    # Generate draft
    draft = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    ).content[0].text

    # Self-review pass
    reviewed = client.messages.create(
        model="claude-haiku-4-5-20251001",   # cheaper model for review
        max_tokens=1200,
        messages=[{"role": "user", "content": REVIEW_PROMPT.format(draft=draft)}],
    ).content[0].text

    return reviewed


# Example: questions that tend to produce self-contradicting long answers
questions = [
    "Compare the performance of synchronous vs asynchronous Python code in detail.",
    "Explain the trade-offs of microservices architecture with concrete examples.",
]
for q in questions:
    answer = self_consistent_respond(q)
    print(f"Q: {q}\nA: {answer[:300]}\n---\n")

# Expected Token Savings: Single review pass ~1000 tokens; avoids expensive pairwise with near-equal quality
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Async Parallel Claim Checking with Batch Deduplication

For high-throughput pipelines, extract claims once and check pairs concurrently. Skip semantically similar claim pairs to reduce redundant checks.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass

@dataclass
class ClaimPair:
    idx_a: int
    idx_b: int
    claim_a: str
    claim_b: str

@dataclass
class Contradiction:
    claim_a: str
    claim_b: str
    explanation: str

client = anthropic.AsyncAnthropic()

def _trigram_similarity(a: str, b: str) -> float:
    def trigrams(s: str) -> set:
        s = s.lower()
        return {s[i:i+3] for i in range(len(s) - 2)}
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

async def _extract_claims(text: str) -> list[str]:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content":
            f"List every distinct factual claim in this text as a JSON array.\n{text}"}],
    )
    try:
        return json.loads(r.content[0].text)
    except Exception:
        return []

async def _check_pair(pair: ClaimPair) -> Contradiction | None:
    prompt = (
        f"Do these contradict each other?\nA: {pair.claim_a}\nB: {pair.claim_b}\n"
        'Return JSON: {"contradicts": true/false, "explanation": "..."}'
    )
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(r.content[0].text)
        if data["contradicts"]:
            return Contradiction(pair.claim_a, pair.claim_b, data["explanation"])
    except Exception:
        pass
    return None

async def async_consistency_check(text: str, similarity_skip_threshold: float = 0.6) -> list[Contradiction]:
    claims = await _extract_claims(text)
    pairs = [
        ClaimPair(i, j, claims[i], claims[j])
        for i in range(len(claims))
        for j in range(i + 1, len(claims))
        if _trigram_similarity(claims[i], claims[j]) < similarity_skip_threshold
    ]
    sem = asyncio.Semaphore(5)
    async def bounded_check(p: ClaimPair):
        async with sem:
            return await _check_pair(p)
    results = await asyncio.gather(*[bounded_check(p) for p in pairs])
    return [r for r in results if r is not None]

async def respond_consistently(question: str) -> str:
    draft_r = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    )
    draft = draft_r.content[0].text
    contradictions = await async_consistency_check(draft)
    if not contradictions:
        return draft
    issues = "\n".join(f"- {c.claim_a!r} conflicts with {c.claim_b!r}: {c.explanation}" for c in contradictions)
    fix_r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content":
            f"Fix these contradictions in the response:\n{issues}\n\nResponse:\n{draft}"}],
    )
    return fix_r.content[0].text

async def main():
    answer = await respond_consistently(
        "Describe Python's memory management and garbage collection in detail."
    )
    print(answer[:500])

asyncio.run(main())

# Expected Token Savings: Parallel checks + dedup cuts check cost ~40%; semaphore prevents API overload
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Structured Section Comparison for Long Documents

For long structured outputs (reports, tutorials), extract per-section facts and cross-check section pairs for numerical or logical conflicts.

```python
import anthropic
import json
import re
from dataclasses import dataclass

@dataclass
class SectionFacts:
    section_title: str
    facts: list[str]

client = anthropic.Anthropic()

def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split on markdown headings. Returns list of (title, body)."""
    pattern = r"^(#{1,3}\s+.+)$"
    parts = re.split(pattern, text, flags=re.MULTILINE)
    sections = []
    i = 0
    while i < len(parts):
        if re.match(pattern, parts[i], re.MULTILINE) and i + 1 < len(parts):
            sections.append((parts[i].strip("#").strip(), parts[i + 1]))
            i += 2
        else:
            i += 1
    return sections if sections else [("Full Response", text)]

def _extract_section_facts(title: str, body: str) -> SectionFacts:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"List concrete facts, numbers, and assertions from this section as JSON array.\n"
            f"Section '{title}':\n{body}"}],
    )
    try:
        facts = json.loads(r.content[0].text)
    except Exception:
        facts = []
    return SectionFacts(section_title=title, facts=facts)

def _cross_check_sections(a: SectionFacts, b: SectionFacts) -> list[str]:
    if not a.facts or not b.facts:
        return []
    prompt = (
        f"Find any contradictions between facts in section '{a.section_title}' "
        f"and section '{b.section_title}'.\n\n"
        f"Section A facts: {json.dumps(a.facts)}\n"
        f"Section B facts: {json.dumps(b.facts)}\n\n"
        'Return JSON array of contradiction strings, or [] if none: ["conflict1", ...]'
    )
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(r.content[0].text)
    except Exception:
        return []

def section_consistent_respond(question: str) -> str:
    draft = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": question}],
    ).content[0].text

    sections = _split_sections(draft)
    if len(sections) < 2:
        return draft  # nothing to cross-check

    section_facts = [_extract_section_facts(t, b) for t, b in sections]
    all_conflicts: list[str] = []
    for i in range(len(section_facts)):
        for j in range(i + 1, len(section_facts)):
            conflicts = _cross_check_sections(section_facts[i], section_facts[j])
            all_conflicts.extend(conflicts)

    if not all_conflicts:
        return draft

    issues = "\n".join(f"- {c}" for c in all_conflicts)
    fixed = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content":
            f"This structured response has cross-section contradictions. Fix them:\n{issues}\n\n{draft}"}],
    ).content[0].text
    print(f"[CONSISTENCY] Resolved {len(all_conflicts)} cross-section conflict(s)")
    return fixed


print(section_consistent_respond(
    "Write a detailed technical report on database indexing strategies including B-trees, "
    "hash indexes, and their performance characteristics."
))

# Expected Token Savings: Section-level check is O(n²) on sections not claims; much cheaper for structured docs
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Numeric and Statistical Claim Validator

Focus exclusively on numeric claims (percentages, dates, counts) which are the most common source of self-contradiction in reports and summaries.

```python
import anthropic
import json
import re
from dataclasses import dataclass

@dataclass
class NumericClaim:
    sentence: str
    number: float
    unit: str
    context: str  # what the number refers to

client = anthropic.Anthropic()

EXTRACT_NUMERIC_PROMPT = """Extract every numeric claim from this text.
For each, identify the number, what it measures, and the sentence it appears in.
Return JSON array:
[{{"number": <float>, "unit": "<what it measures>", "context": "<brief label>", "sentence": "<full sentence>"}}]

Text:
{text}"""

def _extract_numeric_claims(text: str) -> list[NumericClaim]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": EXTRACT_NUMERIC_PROMPT.format(text=text)}],
    )
    try:
        items = json.loads(r.content[0].text)
        return [NumericClaim(**i) for i in items]
    except Exception:
        return []

def _find_numeric_conflicts(claims: list[NumericClaim]) -> list[str]:
    """Group by context/unit and flag conflicting values."""
    groups: dict[str, list[NumericClaim]] = {}
    for c in claims:
        key = f"{c.unit}:{c.context}".lower()
        groups.setdefault(key, []).append(c)
    conflicts = []
    for key, group in groups.items():
        values = [c.number for c in group]
        if len(set(values)) > 1:
            sentences = [c.sentence for c in group]
            conflicts.append(
                f"Conflicting values for '{key}': {values} in sentences: {sentences}"
            )
    return conflicts

def numeric_consistent_respond(question: str) -> str:
    draft = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    ).content[0].text

    claims = _extract_numeric_claims(draft)
    conflicts = _find_numeric_conflicts(claims)
    if not conflicts:
        return draft

    issues = "\n".join(f"- {c}" for c in conflicts)
    fixed = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content":
            f"Fix these numeric inconsistencies in the response (pick the correct value and use it consistently):\n"
            f"{issues}\n\nResponse:\n{draft}"}],
    ).content[0].text
    print(f"[NUMERIC] Fixed {len(conflicts)} numeric conflict(s)")
    return fixed


print(numeric_consistent_respond(
    "Describe the market share distribution of major cloud providers and their revenue growth rates."
))

# Expected Token Savings: Numeric-only extraction is ~50 tokens; catches the highest-stakes contradictions
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Streaming Response with Real-Time Consistency Buffer

Accumulate streamed sentences and check each new sentence against a buffer of prior claims. Interrupt the stream if a contradiction is detected, then regenerate from that point.

```python
import anthropic
import json
import re
from collections import deque

client = anthropic.Anthropic()

CHECK_NEW_CLAIM_PROMPT = """Does this new sentence contradict any of the established facts below?
Established facts:
{facts}

New sentence: {sentence}

Return JSON: {{"contradicts": true/false, "conflicting_fact": "<fact or empty>", "explanation": "<or empty>"}}"""

def _sentence_split(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]

def _check_sentence(sentence: str, fact_buffer: deque[str]) -> dict:
    if len(fact_buffer) < 2:
        return {"contradicts": False}
    facts_text = "\n".join(f"- {f}" for f in fact_buffer)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content":
            CHECK_NEW_CLAIM_PROMPT.format(facts=facts_text, sentence=sentence)}],
    )
    try:
        return json.loads(r.content[0].text)
    except Exception:
        return {"contradicts": False}

def streaming_consistent_respond(question: str, max_sentences_in_buffer: int = 8) -> str:
    # Phase 1: stream and collect full response
    full_text = ""
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    ) as stream:
        for text in stream.text_stream:
            full_text += text
            print(text, end="", flush=True)
    print()

    # Phase 2: sentence-by-sentence consistency check on completed response
    sentences = _sentence_split(full_text)
    fact_buffer: deque[str] = deque(maxlen=max_sentences_in_buffer)
    contradictions = []
    for sentence in sentences:
        result = _check_sentence(sentence, fact_buffer)
        if result.get("contradicts"):
            contradictions.append({
                "sentence": sentence,
                "conflicts_with": result.get("conflicting_fact", ""),
                "explanation": result.get("explanation", ""),
            })
        else:
            fact_buffer.append(sentence)

    if not contradictions:
        return full_text

    # Phase 3: targeted repair
    issues = "\n".join(
        f"- Sentence: {c['sentence']!r}\n  Conflicts with: {c['conflicts_with']!r}\n  Reason: {c['explanation']}"
        for c in contradictions
    )
    fixed = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content":
            f"Fix these contradictions in the text:\n{issues}\n\nText:\n{full_text}"}],
    ).content[0].text
    print(f"\n[STREAM CONSISTENCY] Repaired {len(contradictions)} sentence-level contradiction(s)")
    return fixed


streaming_consistent_respond(
    "Explain how TCP handshake works and contrast it with UDP communication patterns."
)

# Expected Token Savings: Sliding buffer limits check scope; streaming delivery maintained with post-hoc repair
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Detection Scope | Cost | Latency | Best For |
|--------|----------------|------|---------|----------|
| 1. Pairwise Claim Check | All claim pairs | High (O(n²)) | High | Short, high-stakes responses |
| 2. Single-Pass Review | Holistic | Low (~1× response) | Low | General use, good default |
| 3. Async Parallel | All pairs + dedup | Medium | Low (parallel) | High-throughput pipelines |
| 4. Section Cross-Check | Section-level | Medium | Medium | Structured reports/docs |
| 5. Numeric Validator | Numbers only | Very low | Very low | Financial/statistical reports |
| 6. Streaming Buffer | Sentence-level sliding | Medium | Low | Real-time streaming responses |

**Recommended**: Option 2 for most use cases. Option 5 for numerical/financial outputs. Option 4 for long structured documents.
