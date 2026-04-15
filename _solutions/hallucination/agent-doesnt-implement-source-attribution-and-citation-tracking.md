---
layout: solution
title: "Agent Doesn't Implement Source Attribution and Citation Tracking"
category: hallucination
description: "Agent generates responses citing sources but doesn't track which claims came from which source, making it impossible to verify accuracy or provide inline citations to users."
tags: [hallucination, citations, attribution, grounding, audit, retrieval]
---

# Agent Doesn't Implement Source Attribution and Citation Tracking

## Problem

When an agent retrieves documents and generates a response, it may weave together information from multiple sources without tracking which specific claim came from which document. Users receive a fluent answer but have no way to verify individual facts, and the system cannot provide inline citations. Over time this erodes trust and makes the agent unsuitable for research, legal, or medical use cases.

---

## Option 1: Claim Extraction + Source Linking

Extract individual claims from the final response, then ask the model to link each claim to the source that supports it.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class Source:
    id: str
    title: str
    content: str

@dataclass
class AttributedClaim:
    claim: str
    source_id: str
    source_title: str
    quote: str

client = anthropic.Anthropic()

def extract_claims(response: str) -> list[str]:
    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Extract every factual claim from this response as a JSON array of strings.
Only include verifiable factual statements, not opinions or transitions.

Response:
{response}

Return only a JSON array."""
        }]
    )
    text = result.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())

def attribute_claims(claims: list[str], sources: list[Source]) -> list[AttributedClaim]:
    source_block = "\n\n".join(
        f"[{s.id}] {s.title}:\n{s.content}" for s in sources
    )
    result = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"""For each claim, identify which source supports it and provide a short supporting quote.

Sources:
{source_block}

Claims:
{json.dumps(claims, indent=2)}

Return JSON array: [{{"claim": "...", "source_id": "...", "source_title": "...", "quote": "..."}}]
If no source supports a claim, use source_id "UNSUPPORTED"."""
        }]
    )
    text = result.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    raw = json.loads(text.strip())
    return [AttributedClaim(**r) for r in raw]

def answer_with_attribution(query: str, sources: list[Source]) -> dict:
    source_block = "\n\n".join(
        f"[{s.id}] {s.title}:\n{s.content}" for s in sources
    )
    result = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Answer the question using the provided sources.

Sources:
{source_block}

Question: {query}"""
        }]
    )
    response = result.content[0].text
    claims = extract_claims(response)
    attributed = attribute_claims(claims, sources)
    unsupported = [c for c in attributed if c.source_id == "UNSUPPORTED"]
    return {
        "response": response,
        "attributed_claims": [vars(c) for c in attributed],
        "unsupported_count": len(unsupported),
        "coverage": (len(claims) - len(unsupported)) / max(len(claims), 1)
    }

sources = [
    Source("S1", "Climate Report 2024", "Global temperatures rose 1.2°C above pre-industrial levels in 2023."),
    Source("S2", "Energy Review", "Renewable energy now accounts for 30% of global electricity generation."),
]
result = answer_with_attribution("What is the current state of climate change?", sources)
print(f"Coverage: {result['coverage']:.0%}")
print(f"Unsupported claims: {result['unsupported_count']}")

# Expected Token Savings: Baseline (no attribution) ≈ 400 tokens. With claim extraction + linking ≈ 900 tokens total. Overhead ~125% but catches hallucinations before delivery.
# Environment: ANTHROPIC_API_KEY required. No extra packages beyond anthropic.
```

---

## Option 2: Inline Citation Injection

Instruct the model to embed citation markers `[1]`, `[2]` directly in the generated text, then parse them out into a structured reference list.

```python
import anthropic
import re
from dataclasses import dataclass, field

@dataclass
class CitedSource:
    index: int
    title: str
    url: str
    content: str

@dataclass
class CitedResponse:
    text: str
    references: list[CitedSource]
    citation_map: dict  # position -> source index

client = anthropic.Anthropic()

def build_source_prompt(sources: list[CitedSource]) -> str:
    lines = []
    for s in sources:
        lines.append(f"[{s.index}] {s.title} ({s.url})\n{s.content}")
    return "\n\n".join(lines)

def generate_with_inline_citations(query: str, sources: list[CitedSource]) -> CitedResponse:
    source_prompt = build_source_prompt(sources)
    result = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="""You are a research assistant. When you use information from a source,
immediately follow the claim with the citation marker in brackets, e.g. [1] or [2].
Every factual claim must have a citation. Do not make claims without citing a source.""",
        messages=[{
            "role": "user",
            "content": f"""Sources:
{source_prompt}

Question: {query}

Answer with inline citations [N] after each fact."""
        }]
    )
    text = result.content[0].text

    # Extract all citation markers and their positions
    citation_map = {}
    for match in re.finditer(r'\[(\d+)\]', text):
        idx = int(match.group(1))
        pos = match.start()
        citation_map[pos] = idx

    # Identify which sources were actually cited
    cited_indices = set(citation_map.values())
    used_sources = [s for s in sources if s.index in cited_indices]

    return CitedResponse(
        text=text,
        references=used_sources,
        citation_map=citation_map
    )

def format_response(cited: CitedResponse) -> str:
    output = cited.text + "\n\n---\n**References:**\n"
    for s in cited.references:
        output += f"\n[{s.index}] {s.title} — {s.url}"
    return output

sources = [
    CitedSource(1, "WHO Health Report", "https://who.int/report", "Life expectancy globally increased to 73.4 years in 2022."),
    CitedSource(2, "UN Population Data", "https://un.org/population", "World population reached 8 billion in November 2022."),
    CitedSource(3, "CDC Annual Report", "https://cdc.gov/annual", "Heart disease remains the leading cause of death in the US."),
]

cited = generate_with_inline_citations(
    "What are some key global health statistics?",
    sources
)
print(format_response(cited))
print(f"\nCitation markers found: {len(cited.citation_map)}")

# Expected Token Savings: Single-pass inline citations add ~50 tokens to system prompt. Total ≈ 500 tokens vs two-pass attribution at 900. 44% more efficient for high-volume usage.
# Environment: ANTHROPIC_API_KEY required. Uses re module (stdlib).
```

---

## Option 3: Citation Confidence Scoring

After generating a response, score each citation for how well the source actually supports the claim, filtering out low-confidence attributions.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class ScoredCitation:
    claim: str
    source_id: str
    source_title: str
    confidence: float  # 0.0–1.0
    reasoning: str
    accepted: bool

client = anthropic.Anthropic()

CONFIDENCE_THRESHOLD = 0.7

def score_citations(
    claims_with_sources: list[dict],
    sources: dict[str, str]
) -> list[ScoredCitation]:
    """
    claims_with_sources: [{"claim": "...", "source_id": "..."}]
    sources: {"S1": "full source text", ...}
    """
    items = []
    for item in claims_with_sources:
        source_text = sources.get(item["source_id"], "")
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"""Rate how well this source supports this claim.

Claim: {item['claim']}

Source text: {source_text}

Return JSON: {{"confidence": 0.0-1.0, "reasoning": "one sentence"}}
1.0 = directly stated, 0.5 = implied, 0.0 = contradicted or absent."""
            }]
        )
        text = result.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        scored = json.loads(text.strip())
        items.append(ScoredCitation(
            claim=item["claim"],
            source_id=item["source_id"],
            source_title=item.get("source_title", item["source_id"]),
            confidence=scored["confidence"],
            reasoning=scored["reasoning"],
            accepted=scored["confidence"] >= CONFIDENCE_THRESHOLD
        ))
    return items

def filter_and_report(citations: list[ScoredCitation]) -> dict:
    accepted = [c for c in citations if c.accepted]
    rejected = [c for c in citations if not c.accepted]
    return {
        "accepted": [vars(c) for c in accepted],
        "rejected_as_unsupported": [vars(c) for c in rejected],
        "acceptance_rate": len(accepted) / max(len(citations), 1),
        "avg_confidence": sum(c.confidence for c in citations) / max(len(citations), 1)
    }

sources = {
    "S1": "The Eiffel Tower was built in 1889 and stands 330 meters tall including its antenna.",
    "S2": "Paris has a population of approximately 2.1 million in the city proper.",
}

candidates = [
    {"claim": "The Eiffel Tower was constructed in 1889.", "source_id": "S1", "source_title": "Eiffel Tower Facts"},
    {"claim": "The Eiffel Tower is the tallest structure in the world.", "source_id": "S1", "source_title": "Eiffel Tower Facts"},
    {"claim": "Paris has over 2 million residents.", "source_id": "S2", "source_title": "Paris Demographics"},
]

citations = score_citations(candidates, sources)
report = filter_and_report(citations)
print(f"Acceptance rate: {report['acceptance_rate']:.0%}")
print(f"Avg confidence: {report['avg_confidence']:.2f}")
print(f"Rejected: {len(report['rejected_as_unsupported'])} claims")

# Expected Token Savings: Per-claim scoring with haiku uses ~150 tokens each. For 10 claims: 1500 tokens. Prevents hallucinated citations from reaching users, reducing support burden.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 4: Grounded Generation with Forced Citations

Use a structured output format that forces the model to ground every sentence in a source before writing it, preventing hallucination at generation time.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class GroundedSentence:
    sentence: str
    source_ids: list[str]
    direct_quote: str

@dataclass
class GroundedResponse:
    sentences: list[GroundedSentence]
    ungrounded_count: int

    def to_text(self) -> str:
        return " ".join(s.sentence for s in self.sentences)

    def to_annotated(self) -> str:
        parts = []
        for s in self.sentences:
            if s.source_ids:
                tags = "".join(f"[{sid}]" for sid in s.source_ids)
                parts.append(f"{s.sentence} {tags}")
            else:
                parts.append(f"{s.sentence} [UNGROUNDED]")
        return " ".join(parts)

client = anthropic.Anthropic()

def generate_grounded_response(query: str, sources: dict[str, str]) -> GroundedResponse:
    source_block = "\n\n".join(
        f"[{sid}]: {text}" for sid, text in sources.items()
    )
    result = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system="""You are a grounded research assistant. You MUST structure your entire response
as a JSON array where each element is one sentence with its citations.
Never write a sentence you cannot ground in the provided sources.
Format: [{"sentence": "...", "source_ids": ["S1"], "direct_quote": "exact supporting text"}]""",
        messages=[{
            "role": "user",
            "content": f"""Sources:
{source_block}

Question: {query}

Respond ONLY with the JSON array of grounded sentences."""
        }]
    )
    text = result.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    raw = json.loads(text.strip())
    sentences = []
    ungrounded = 0
    for item in raw:
        gs = GroundedSentence(
            sentence=item["sentence"],
            source_ids=item.get("source_ids", []),
            direct_quote=item.get("direct_quote", "")
        )
        if not gs.source_ids:
            ungrounded += 1
        sentences.append(gs)

    return GroundedResponse(sentences=sentences, ungrounded_count=ungrounded)

sources = {
    "S1": "Photosynthesis converts light energy into chemical energy stored in glucose.",
    "S2": "Chlorophyll, the green pigment in plants, absorbs light primarily in the red and blue wavelengths.",
    "S3": "The process of photosynthesis releases oxygen as a byproduct.",
}

grounded = generate_grounded_response("How does photosynthesis work?", sources)
print("Annotated response:")
print(grounded.to_annotated())
print(f"\nUngrounded sentences: {grounded.ungrounded_count}")
print(f"Total sentences: {len(grounded.sentences)}")

# Expected Token Savings: Grounded generation adds ~100 token system prompt overhead. Eliminates post-hoc attribution pass entirely, saving 400–600 tokens vs two-pass methods for responses under 500 words.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 5: Post-Hoc Attribution Matching

Generate the response naturally, then use embedding-free fuzzy matching to link response segments back to source passages.

```python
import anthropic
import json
import re
from dataclasses import dataclass

@dataclass
class AttributionMatch:
    segment: str
    best_source_id: str
    best_source_title: str
    overlap_score: float
    matched_phrase: str

client = anthropic.Anthropic()

def tokenize(text: str) -> set[str]:
    words = re.findall(r'\b[a-z]{3,}\b', text.lower())
    # Build bigrams too
    bigrams = {f"{words[i]} {words[i+1]}" for i in range(len(words)-1)}
    return set(words) | bigrams

def overlap_score(segment: str, source: str) -> tuple[float, str]:
    seg_tokens = tokenize(segment)
    src_tokens = tokenize(source)
    if not seg_tokens:
        return 0.0, ""
    intersection = seg_tokens & src_tokens
    score = len(intersection) / len(seg_tokens)
    # Find the longest matching phrase
    best_phrase = max(intersection, key=len) if intersection else ""
    return score, best_phrase

def split_into_segments(text: str) -> list[str]:
    # Split on sentence boundaries
    segments = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in segments if len(s.strip()) > 20]

def attribute_post_hoc(
    response: str,
    sources: dict[str, dict]  # {"S1": {"title": "...", "content": "..."}}
) -> list[AttributionMatch]:
    segments = split_into_segments(response)
    matches = []
    for segment in segments:
        best_score = 0.0
        best_source_id = "UNATTRIBUTED"
        best_source_title = "None"
        best_phrase = ""
        for sid, sdata in sources.items():
            score, phrase = overlap_score(segment, sdata["content"])
            if score > best_score:
                best_score = score
                best_source_id = sid
                best_source_title = sdata["title"]
                best_phrase = phrase
        if best_score < 0.15:
            best_source_id = "UNATTRIBUTED"
        matches.append(AttributionMatch(
            segment=segment,
            best_source_id=best_source_id,
            best_source_title=best_source_title,
            overlap_score=best_score,
            matched_phrase=best_phrase
        ))
    return matches

def generate_and_attribute(query: str, sources: dict[str, dict]) -> dict:
    source_block = "\n\n".join(
        f"{data['title']}:\n{data['content']}"
        for data in sources.values()
    )
    result = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Sources:\n{source_block}\n\nQuestion: {query}"
        }]
    )
    response = result.content[0].text
    matches = attribute_post_hoc(response, sources)
    unattributed = [m for m in matches if m.best_source_id == "UNATTRIBUTED"]
    return {
        "response": response,
        "attribution": [vars(m) for m in matches],
        "unattributed_segments": len(unattributed),
        "attribution_rate": (len(matches) - len(unattributed)) / max(len(matches), 1)
    }

sources = {
    "S1": {"title": "Solar Energy Basics", "content": "Solar panels convert sunlight into electricity using photovoltaic cells made of silicon."},
    "S2": {"title": "Energy Storage", "content": "Lithium-ion batteries store excess solar energy for use during nighttime or cloudy periods."},
}
result = generate_and_attribute("How does solar energy work?", sources)
print(f"Attribution rate: {result['attribution_rate']:.0%}")
print(f"Unattributed segments: {result['unattributed_segments']}")

# Expected Token Savings: Zero extra LLM calls for attribution — pure Python fuzzy matching. Saves 400–900 tokens vs LLM-based attribution. Trade-off: lower precision than semantic matching.
# Environment: ANTHROPIC_API_KEY required. Uses re module (stdlib only).
```

---

## Option 6: Audit Trail with SQLite Citation Log

Persist every claim-to-source mapping in SQLite, enabling downstream auditing, citation analytics, and hallucination trend monitoring.

```python
import anthropic
import json
import sqlite3
import uuid
from datetime import datetime
from dataclasses import dataclass

@dataclass
class CitationRecord:
    citation_id: str
    session_id: str
    query: str
    claim: str
    source_id: str
    source_title: str
    confidence: float
    model: str
    created_at: str

client = anthropic.Anthropic()

def init_db(db_path: str = "citations.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS citations (
            citation_id TEXT PRIMARY KEY,
            session_id TEXT,
            query TEXT,
            claim TEXT,
            source_id TEXT,
            source_title TEXT,
            confidence REAL,
            model TEXT,
            created_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON citations(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON citations(source_id)")
    conn.commit()
    return conn

def extract_and_log_citations(
    query: str,
    response: str,
    sources: list[dict],
    session_id: str,
    conn: sqlite3.Connection,
    model: str = "claude-sonnet-4-6"
) -> list[CitationRecord]:
    source_block = "\n\n".join(
        f"[{s['id']}] {s['title']}: {s['content']}" for s in sources
    )
    result = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Extract claims from this response and attribute each to a source.

Sources:
{source_block}

Response to analyze:
{response}

Return JSON array:
[{{"claim": "...", "source_id": "...", "source_title": "...", "confidence": 0.0-1.0}}]
Use source_id "NONE" and confidence 0.0 for unsupported claims."""
        }]
    )
    text = result.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    raw = json.loads(text.strip())

    records = []
    now = datetime.utcnow().isoformat()
    for item in raw:
        record = CitationRecord(
            citation_id=str(uuid.uuid4()),
            session_id=session_id,
            query=query,
            claim=item["claim"],
            source_id=item["source_id"],
            source_title=item.get("source_title", ""),
            confidence=item.get("confidence", 0.0),
            model=model,
            created_at=now
        )
        conn.execute(
            """INSERT INTO citations VALUES (?,?,?,?,?,?,?,?,?)""",
            (record.citation_id, record.session_id, record.query,
             record.claim, record.source_id, record.source_title,
             record.confidence, record.model, record.created_at)
        )
        records.append(record)
    conn.commit()
    return records

def get_session_audit(session_id: str, conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT * FROM citations WHERE session_id=?", (session_id,)
    ).fetchall()
    total = len(rows)
    unsupported = sum(1 for r in rows if r[4] == "NONE")
    avg_confidence = sum(r[6] for r in rows) / max(total, 1)
    source_counts = {}
    for r in rows:
        source_counts[r[5]] = source_counts.get(r[5], 0) + 1
    return {
        "session_id": session_id,
        "total_claims": total,
        "unsupported_claims": unsupported,
        "hallucination_rate": unsupported / max(total, 1),
        "avg_confidence": avg_confidence,
        "source_usage": source_counts
    }

# Demo
conn = init_db(":memory:")
session_id = str(uuid.uuid4())
sources = [
    {"id": "S1", "title": "Space Report", "content": "The James Webb Space Telescope launched on December 25, 2021."},
    {"id": "S2", "title": "NASA Overview", "content": "NASA's budget for 2024 was approximately $25.4 billion."},
]
query = "Tell me about recent NASA milestones."
gen_result = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=256,
    messages=[{"role": "user", "content": f"Sources: {json.dumps(sources)}\n\nQuestion: {query}"}]
)
response = gen_result.content[0].text
records = extract_and_log_citations(query, response, sources, session_id, conn)
audit = get_session_audit(session_id, conn)
print(f"Session hallucination rate: {audit['hallucination_rate']:.0%}")
print(f"Total claims logged: {audit['total_claims']}")
print(f"Source usage: {audit['source_usage']}")

# Expected Token Savings: Attribution call uses ~600 tokens per session. SQLite logging adds zero tokens. Enables long-term hallucination analytics and model comparison across sessions.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3 (stdlib). DB path configurable.
```

---

## Comparison

| Option | Attribution Method | Token Cost | Precision | Persistence | Best For |
|--------|-------------------|------------|-----------|-------------|----------|
| 1: Claim Extraction + Linking | Two-pass LLM | ~900 | High | None | General RAG pipelines |
| 2: Inline Citation Injection | Single-pass with format | ~500 | Medium-High | None | User-facing chat with footnotes |
| 3: Citation Confidence Scoring | Per-claim haiku scoring | ~150/claim | High | None | High-stakes verification |
| 4: Grounded Generation | Structured JSON output | ~400 | Highest | None | Research / legal / medical |
| 5: Post-Hoc Fuzzy Matching | Zero LLM calls | 0 extra | Medium | None | High-throughput, cost-sensitive |
| 6: SQLite Audit Trail | LLM + SQLite log | ~600 | High | SQLite | Compliance, audit, analytics |
