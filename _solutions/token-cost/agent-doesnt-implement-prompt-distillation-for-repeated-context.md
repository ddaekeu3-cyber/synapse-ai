---
layout: solution
title: "Agent Doesn't Implement Prompt Distillation for Repeated Context"
category: token-cost
description: "Distill verbose, repeated context into compact summaries before re-injecting it on every turn, dramatically reducing input token costs for long-running or multi-turn agent sessions."
tags: [token-cost, prompt-distillation, context-compression, summarization, cost-optimization, multi-turn, caching]
---

# Agent Doesn't Implement Prompt Distillation for Repeated Context

## Problem

Agents that re-inject the same large context block on every turn — a lengthy policy document, a large tool result, or a growing conversation history — pay full input token cost each time. A 2,000-token context repeated across 50 turns costs 100,000 input tokens for what is effectively static information. Distilling that context into a compact summary pays the cost once and amortizes it across the entire session.

## Solution Options

### Option 1: One-Shot Summarization Before Re-injection

```python
import anthropic


def distill(client: anthropic.Anthropic, verbose_context: str, max_summary_tokens: int = 200) -> str:
    """Compress verbose context into a compact summary."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_summary_tokens,
        messages=[{
            "role": "user",
            "content": (
                f"Summarize the following into a concise reference (under {max_summary_tokens} tokens). "
                f"Preserve all key facts, numbers, and decisions.\n\n{verbose_context}"
            ),
        }],
    )
    return resp.content[0].text.strip()


def multi_turn_agent(context: str, questions: list[str]) -> list[str]:
    client = anthropic.Anthropic()

    # Distill once — pay cost one time
    distilled = distill(client, context)
    original_tokens = len(context.split())
    distilled_tokens = len(distilled.split())
    print(f"[distill] {original_tokens} words → {distilled_tokens} words "
          f"({100 * (1 - distilled_tokens / original_tokens):.0f}% reduction)")

    answers = []
    for q in questions:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"Reference context:\n{distilled}",
            messages=[{"role": "user", "content": q}],
        )
        answers.append(resp.content[0].text.strip())

    return answers


if __name__ == "__main__":
    # Simulate a verbose policy document repeated on every turn
    verbose_doc = """
    ACME Corporation Employee Policy Manual v3.2 — Effective January 1, 2025.
    Section 1: Working Hours. Standard hours are 9am–5pm Monday–Friday.
    Overtime requires manager approval and is paid at 1.5x for hours over 40/week.
    Section 2: Vacation. Employees accrue 1.5 days per month (18 days/year).
    Unused vacation may be carried over up to 5 days. No payout on termination.
    Section 3: Remote Work. Up to 2 days per week with manager approval.
    Section 4: Equipment. Laptops replaced every 3 years. $500 home office stipend.
    Section 5: Health Benefits. Medical, dental, vision covered at 80% for employees.
    Dependents covered at 60%. Open enrollment in November each year.
    """ * 5  # repeat to simulate a real large document

    questions = [
        "How many vacation days do employees get per year?",
        "What is the remote work policy?",
        "When is open enrollment?",
    ]

    answers = multi_turn_agent(verbose_doc, questions)
    for q, a in zip(questions, answers):
        print(f"Q: {q}\nA: {a[:100]}\n")

# Expected Token Savings: ~70–85% reduction in repeated context tokens across multi-turn sessions
# Environment: Agents that re-inject static documents, policies, or tool outputs on every turn
```

---

### Option 2: Incremental Distillation with Decay

```python
import anthropic
from dataclasses import dataclass, field


@dataclass
class DistilledContext:
    summary: str
    source_word_count: int
    summary_word_count: int
    turn_created: int
    last_updated: int

    @property
    def compression_ratio(self) -> float:
        return 1 - self.summary_word_count / max(self.source_word_count, 1)


class IncrementalDistiller:
    """
    Maintains a rolling distilled context.
    Re-distills when new information arrives or after N turns of staleness.
    """

    REFRESH_EVERY_N_TURNS = 10
    MAX_SUMMARY_WORDS = 150

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._context: DistilledContext | None = None
        self._turn = 0

    def _distill(self, text: str, existing_summary: str | None = None) -> str:
        preamble = (
            f"Existing summary:\n{existing_summary}\n\nNew information to incorporate:\n"
            if existing_summary
            else ""
        )
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=self.MAX_SUMMARY_WORDS * 2,
            messages=[{
                "role": "user",
                "content": (
                    f"{preamble}{text}\n\n"
                    f"Produce a single concise summary under {self.MAX_SUMMARY_WORDS} words "
                    f"preserving all key facts."
                ),
            }],
        )
        return resp.content[0].text.strip()

    def ingest(self, new_text: str) -> DistilledContext:
        existing = self._context.summary if self._context else None
        summary = self._distill(new_text, existing)
        self._context = DistilledContext(
            summary=summary,
            source_word_count=len(new_text.split()) + (self._context.source_word_count if self._context else 0),
            summary_word_count=len(summary.split()),
            turn_created=self._turn,
            last_updated=self._turn,
        )
        return self._context

    def get_context(self) -> str:
        if self._context is None:
            return ""
        return self._context.summary

    def ask(self, question: str) -> str:
        self._turn += 1
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"Context:\n{self.get_context()}",
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text.strip()


if __name__ == "__main__":
    distiller = IncrementalDistiller()

    # Ingest new context incrementally
    distiller.ingest("Project Alpha: budget $500K, deadline Q3 2025, team of 8.")
    distiller.ingest("Risk identified: third-party API dependency may delay integration by 3 weeks.")
    ctx = distiller.ingest("Stakeholder approved 2-week extension. New deadline: October 15, 2025.")

    print(f"Compression: {ctx.compression_ratio:.0%}")
    print(f"Summary ({ctx.summary_word_count} words): {ctx.summary}")

    questions = ["What is the project deadline?", "What risks were identified?"]
    for q in questions:
        print(f"\nQ: {q}\nA: {distiller.ask(q)}")

# Expected Token Savings: Incremental merge avoids full re-summarization; only diffs are processed
# Environment: Long-running agent sessions accumulating updates over time
```

---

### Option 3: Cache-Aware Distillation with Prompt Caching

```python
import anthropic
from dataclasses import dataclass


@dataclass
class CachedDistilledContext:
    summary: str
    cache_tokens_written: int = 0
    cache_tokens_read: int = 0
    api_calls: int = 0


class CacheAwareDistiller:
    """
    Distills verbose context once, then uses Anthropic prompt caching
    so the distilled summary itself is cached for subsequent turns.
    Double savings: fewer tokens AND cache hits.
    """

    SYSTEM_PREAMBLE = (
        "You are a knowledgeable assistant. Use the provided context to answer questions accurately. "
        "If the context doesn't contain the answer, say so clearly."
    )

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._cached_ctx: CachedDistilledContext | None = None

    def prepare(self, verbose_context: str) -> CachedDistilledContext:
        """Distill and cache the context — call once at session start."""
        # Step 1: Distill
        distill_resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"Summarize this into ≤200 words, preserving all facts:\n\n{verbose_context}",
            }],
        )
        summary = distill_resp.content[0].text.strip()

        # Step 2: Warm the cache with a preflight message
        warm_resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            system=[
                {"type": "text", "text": self.SYSTEM_PREAMBLE},
                {"type": "text", "text": f"\n\nContext:\n{summary}", "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": "ready"}],
        )
        self._cached_ctx = CachedDistilledContext(
            summary=summary,
            cache_tokens_written=warm_resp.usage.cache_creation_input_tokens or 0,
            api_calls=1,
        )
        return self._cached_ctx

    def ask(self, question: str) -> tuple[str, bool]:
        """Returns (answer, cache_hit)."""
        if not self._cached_ctx:
            raise RuntimeError("Call prepare() first")

        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=[
                {"type": "text", "text": self.SYSTEM_PREAMBLE},
                {"type": "text", "text": f"\n\nContext:\n{self._cached_ctx.summary}",
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": question}],
        )
        cache_hit = (resp.usage.cache_read_input_tokens or 0) > 0
        if cache_hit:
            self._cached_ctx.cache_tokens_read += resp.usage.cache_read_input_tokens or 0
        self._cached_ctx.api_calls += 1
        return resp.content[0].text.strip(), cache_hit


if __name__ == "__main__":
    verbose = (
        "TechCorp Q1 2025 Financial Report: Revenue $12.4M (+18% YoY). "
        "Net income $2.1M. Operating expenses $9.8M. R&D spend $3.2M. "
        "Headcount 142 (+12 from Q4). Top product: CloudSync ($7.2M revenue). "
        "Geographic: 60% North America, 30% Europe, 10% APAC. "
        "Guidance Q2: revenue $13–14M. No dividend declared."
    ) * 8  # inflate to ~realistic doc size

    distiller = CacheAwareDistiller()
    ctx = distiller.prepare(verbose)
    print(f"Distilled to {len(ctx.summary.split())} words. Cache written: {ctx.cache_tokens_written} tokens")

    questions = [
        "What was the revenue growth?",
        "What is the Q2 guidance?",
        "What is the top product?",
    ]
    for q in questions:
        answer, hit = distiller.ask(q)
        print(f"[cache_hit={hit}] Q: {q}\n  A: {answer[:80]}")

# Expected Token Savings: Distillation + caching = ~90% total input token reduction for 10+ turns
# Environment: Report analysis agents, document Q&A bots with high turn volume
```

---

### Option 4: Hierarchical Distillation for Multi-Document Contexts

```python
import anthropic
from dataclasses import dataclass


@dataclass
class DocumentSummary:
    source_id: str
    title: str
    summary: str
    word_count: int


class HierarchicalDistiller:
    """
    Two-level distillation:
    Level 1: Summarize each document independently.
    Level 2: Merge all document summaries into a single unified brief.
    Maintains provenance so the agent can cite which document an answer came from.
    """

    def __init__(self, summary_words_per_doc: int = 80, merged_words: int = 200) -> None:
        self._client = anthropic.Anthropic()
        self.summary_words_per_doc = summary_words_per_doc
        self.merged_words = merged_words
        self._doc_summaries: list[DocumentSummary] = []
        self._merged: str = ""

    def add_document(self, source_id: str, title: str, content: str) -> DocumentSummary:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=self.summary_words_per_doc * 2,
            messages=[{
                "role": "user",
                "content": (
                    f"Summarize this document in ≤{self.summary_words_per_doc} words. "
                    f"Preserve specific facts, numbers, and names.\n\nTitle: {title}\n\n{content}"
                ),
            }],
        )
        summary = DocumentSummary(
            source_id=source_id,
            title=title,
            summary=resp.content[0].text.strip(),
            word_count=len(resp.content[0].text.split()),
        )
        self._doc_summaries.append(summary)
        return summary

    def merge(self) -> str:
        if not self._doc_summaries:
            return ""
        combined = "\n\n".join(
            f"[{s.source_id}] {s.title}:\n{s.summary}"
            for s in self._doc_summaries
        )
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=self.merged_words * 2,
            messages=[{
                "role": "user",
                "content": (
                    f"Merge these summaries into a unified brief of ≤{self.merged_words} words. "
                    f"Keep source references [doc_id] inline.\n\n{combined}"
                ),
            }],
        )
        self._merged = resp.content[0].text.strip()
        return self._merged

    def ask(self, question: str) -> str:
        if not self._merged:
            self.merge()
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"Unified document brief:\n{self._merged}",
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text.strip()


if __name__ == "__main__":
    distiller = HierarchicalDistiller(summary_words_per_doc=60, merged_words=150)

    docs = [
        ("DOC1", "Q1 Sales Report", "Sales team closed 42 deals in Q1. Top rep: Sarah Chen with $1.2M. Pipeline grew 30%."),
        ("DOC2", "Engineering Update", "Shipped v2.3 on Feb 15. Bug count reduced 40%. Next milestone: v2.4 with AI features in April."),
        ("DOC3", "HR Summary", "Hired 8 engineers and 2 PMs. Attrition 4%. Remote-first policy extended through 2025."),
    ]
    for sid, title, content in docs:
        s = distiller.add_document(sid, title, content * 5)  # inflate for realism
        print(f"[{sid}] {s.word_count} words")

    merged = distiller.merge()
    print(f"\nMerged brief ({len(merged.split())} words):\n{merged}\n")

    for q in ["Who was the top sales rep?", "When is v2.4 shipping?"]:
        print(f"Q: {q}\nA: {distiller.ask(q)}\n")

# Expected Token Savings: N-doc reduction: each doc pays summary cost once; merged brief used thereafter
# Environment: Multi-document research agents, due diligence bots, competitive intelligence tools
```

---

### Option 5: Lossy vs. Lossless Distillation with Fidelity Control

```python
import anthropic
from enum import Enum


class FidelityLevel(Enum):
    HIGH = "high"      # near-lossless, larger summary
    MEDIUM = "medium"  # balanced
    LOW = "low"        # aggressive compression, headline only


FIDELITY_CONFIG = {
    FidelityLevel.HIGH:   {"max_words": 400, "instruction": "Preserve all facts, numbers, names, and decisions. Miss nothing important."},
    FidelityLevel.MEDIUM: {"max_words": 150, "instruction": "Preserve key facts and decisions. Omit repetition and boilerplate."},
    FidelityLevel.LOW:    {"max_words": 50,  "instruction": "Give a 1–2 sentence headline summary only."},
}


def distill_with_fidelity(
    client: anthropic.Anthropic,
    context: str,
    fidelity: FidelityLevel = FidelityLevel.MEDIUM,
) -> str:
    cfg = FIDELITY_CONFIG[fidelity]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=cfg["max_words"] * 2,
        messages=[{
            "role": "user",
            "content": (
                f"Summarize in ≤{cfg['max_words']} words. {cfg['instruction']}\n\n{context}"
            ),
        }],
    )
    return resp.content[0].text.strip()


def adaptive_distiller(context: str, question: str, token_budget: int = 500) -> str:
    """
    Chooses fidelity level based on available token budget.
    High budget → high fidelity. Tight budget → aggressive compression.
    """
    client = anthropic.Anthropic()

    ctx_tokens_estimate = len(context.split()) * 1.3  # rough tokens
    if token_budget > ctx_tokens_estimate * 0.8:
        fidelity = FidelityLevel.HIGH
    elif token_budget > 200:
        fidelity = FidelityLevel.MEDIUM
    else:
        fidelity = FidelityLevel.LOW

    summary = distill_with_fidelity(client, context, fidelity)
    print(f"[fidelity={fidelity.value}] {len(context.split())} words → {len(summary.split())} words")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(token_budget, 256),
        system=f"Context:\n{summary}",
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text.strip()


if __name__ == "__main__":
    large_context = (
        "The merger agreement was signed on March 3, 2025. "
        "Acquirer: GlobalTech Inc. Target: NanoSoft Ltd. "
        "Deal value: $450M (70% cash, 30% stock). "
        "Closing expected Q3 2025 pending regulatory approval. "
        "NanoSoft CEO will join GlobalTech as CTO. "
        "200 NanoSoft employees to be retained; 50 redundancies expected. "
    ) * 20

    question = "What is the deal value and when does it close?"

    for budget in [2000, 300, 80]:
        print(f"\n--- Budget: {budget} tokens ---")
        answer = adaptive_distiller(large_context, question, token_budget=budget)
        print(f"A: {answer}")

# Expected Token Savings: LOW fidelity uses ~90% fewer tokens; fidelity adapts to available budget
# Environment: Agents with dynamic token budgets (e.g., mid-conversation context window pressure)
```

---

### Option 6: Distillation Pipeline with Quality Verification

```python
import anthropic
from dataclasses import dataclass


@dataclass
class DistillationResult:
    original_words: int
    summary_words: int
    summary: str
    quality_score: float  # 0.0–1.0
    quality_issues: list[str]

    @property
    def compression_ratio(self) -> float:
        return 1 - self.summary_words / max(self.original_words, 1)


class VerifiedDistiller:
    """
    Distills context and then verifies the summary retains key facts.
    If quality is too low, re-distills with higher fidelity.
    """

    MIN_QUALITY = 0.7
    MAX_RETRIES = 2

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()

    def _summarize(self, text: str, max_words: int) -> str:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_words * 2,
            messages=[{
                "role": "user",
                "content": f"Summarize in ≤{max_words} words, preserving all key facts and numbers:\n\n{text}",
            }],
        )
        return resp.content[0].text.strip()

    def _verify(self, original: str, summary: str) -> tuple[float, list[str]]:
        """Ask the model to score the summary's fidelity."""
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Original:\n{original[:2000]}\n\nSummary:\n{summary}\n\n"
                    "List any important facts missing from the summary (one per line). "
                    "Then on the last line write: SCORE: 0.X (0=poor, 1=perfect). "
                    "If nothing is missing, just write: SCORE: 1.0"
                ),
            }],
        )
        text = resp.content[0].text
        score = 0.8
        issues = []
        for line in text.splitlines():
            if line.startswith("SCORE:"):
                try:
                    score = float(line.split(":")[1].strip())
                except ValueError:
                    pass
            elif line.strip() and not line.startswith("SCORE"):
                issues.append(line.strip())
        return score, issues

    def distill(self, text: str, target_words: int = 150) -> DistillationResult:
        max_words = target_words
        for attempt in range(self.MAX_RETRIES + 1):
            summary = self._summarize(text, max_words)
            score, issues = self._verify(text, summary)
            if score >= self.MIN_QUALITY:
                break
            # Retry with more room
            max_words = int(max_words * 1.5)
            print(f"[distill] Attempt {attempt + 1} quality={score:.2f} — retrying with {max_words} words")

        return DistillationResult(
            original_words=len(text.split()),
            summary_words=len(summary.split()),
            summary=summary,
            quality_score=score,
            quality_issues=issues,
        )


if __name__ == "__main__":
    client = anthropic.Anthropic()
    distiller = VerifiedDistiller()

    context = (
        "Contract terms: Vendor: CloudBase Inc. Duration: 24 months from April 1, 2025. "
        "Annual fee: $240,000 (payable quarterly). SLA: 99.9% uptime. "
        "Penalty: 10% fee credit per 0.1% below SLA. "
        "Data residency: US-East only. Termination: 90 days notice. "
        "Renewal: auto-renews unless cancelled 60 days before expiry. "
        "IP: all custom integrations owned by customer. "
    ) * 6

    result = distiller.distill(context, target_words=100)
    print(f"Compression: {result.compression_ratio:.0%}")
    print(f"Quality: {result.quality_score:.2f}")
    if result.quality_issues:
        print(f"Issues: {result.quality_issues}")
    print(f"\nSummary:\n{result.summary}")

    # Use summary in follow-up
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=f"Contract summary:\n{result.summary}",
        messages=[{"role": "user", "content": "What is the termination notice period?"}],
    )
    print(f"\nQ: What is the termination notice period?\nA: {resp.content[0].text}")

# Expected Token Savings: Verified compression; retries only when quality is insufficient
# Environment: Legal, compliance, or contract review agents where accuracy is non-negotiable
```

---

## Comparison

| Option | Approach | Best For | Compression | Verification |
|--------|----------|----------|-------------|--------------|
| 1 | One-shot distillation | Quick static context compression | 70–85% | None |
| 2 | Incremental merge on new info | Accumulating session state | 60–80% | None |
| 3 | Distillation + prompt caching | High turn-volume Q&A bots | ~90% combined | None |
| 4 | Hierarchical multi-document merge | Multi-source research agents | Per-doc + merged | None |
| 5 | Adaptive fidelity by token budget | Budget-constrained sessions | Up to 95% (LOW) | None |
| 6 | Distillation with quality verification | Legal/compliance accuracy requirements | 70–80% | LLM-scored |
