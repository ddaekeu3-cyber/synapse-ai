---
layout: solution
title: "Agent Doesn't Implement Chain-of-Density Summarization"
category: prompt-engineering
description: "Agents that summarize in a single pass produce either too-long or too-sparse outputs. Chain-of-Density (CoD) iteratively rewrites the summary to be the same length but progressively denser — adding more entities and details per word until the target information density is achieved, without increasing token count."
tags: [summarization, chain-of-density, compression, information-density, iterative, prompt-engineering, context-window]
---

# Agent Doesn't Implement Chain-of-Density Summarization

## Problem

Single-pass summarization has an accuracy-brevity trade-off: a short summary misses key facts, a long summary wastes tokens. Chain-of-Density (Adams et al., 2023) resolves this by iteratively fusing more information into the same word count. Each iteration identifies missing entities, rewrites the summary to include them without lengthening it, and produces a denser result. After 3–5 iterations, the summary contains far more information per token than a single-pass approach, while remaining readable.

**Symptoms:**
- Summaries miss critical entities present in the source document
- Long summaries waste tokens in downstream LLM calls
- Short summaries lose too much detail to be actionable
- No control over information density vs. readability trade-off
- Summary quality varies unpredictably across different document lengths

---

## Option 1: Basic Chain-of-Density with Fixed Iterations

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class DensitySummary:
    iteration: int
    summary: str
    word_count: int
    new_entities: list[str]

def chain_of_density_summarize(
    text: str,
    target_word_count: int = 80,
    iterations: int = 4
) -> list[DensitySummary]:
    client = anthropic.Anthropic()
    results = []

    # Iteration 1: initial sparse summary
    system = f"""Summarize the article in approximately {target_word_count} words.
The summary should be informative but may be vague."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": f"Article:\n{text}"}]
    )
    current_summary = response.content[0].text.strip()
    word_count = len(current_summary.split())
    results.append(DensitySummary(1, current_summary, word_count, []))
    print(f"Iteration 1 ({word_count} words): {current_summary[:80]}...")

    # Iterations 2–N: add entities without increasing length
    for i in range(2, iterations + 1):
        cod_prompt = f"""Article:
{text}

Current summary:
{current_summary}

Identify up to 3 important entities, events, or facts from the article that are missing from the current summary.
Then rewrite the summary to include these missing items WITHOUT making the summary longer (keep it ~{target_word_count} words).
Achieve this by fusing information and using more concise phrasing.

Format your response exactly as:
MISSING: [comma-separated list of missing items]
SUMMARY: [rewritten summary]"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": cod_prompt}]
        )
        raw = response.content[0].text.strip()

        # Parse response
        missing_line = ""
        summary_line = current_summary
        for line in raw.split("\n"):
            if line.startswith("MISSING:"):
                missing_line = line[8:].strip()
            elif line.startswith("SUMMARY:"):
                summary_line = line[8:].strip()

        entities = [e.strip() for e in missing_line.split(",") if e.strip()]
        current_summary = summary_line
        word_count = len(current_summary.split())
        results.append(DensitySummary(i, current_summary, word_count, entities))
        print(f"Iteration {i} ({word_count} words, +{len(entities)} entities): {current_summary[:80]}...")

    return results

# Test document
article = """
The James Webb Space Telescope (JWST), launched on December 25, 2021, has revolutionized
our understanding of the early universe. Operating at L2 Lagrange point, 1.5 million
kilometers from Earth, it uses infrared imaging to peer through dust clouds invisible to
optical telescopes. In 2023, JWST detected JADES-GS-z14-0, the most distant galaxy ever
observed, formed just 290 million years after the Big Bang, with a redshift of z=14.32.
The telescope's 6.5-meter beryllium mirror, 100 times more powerful than Hubble's, operates
at -233°C. Key findings include direct imaging of exoplanet atmospheres, detection of
carbon dioxide on TRAPPIST-1e, and the discovery of 717 new galaxy candidates in the
Hubble Ultra Deep Field. NASA's $10 billion investment has already produced over 6,000
peer-reviewed papers in two years of operation.
"""

summaries = chain_of_density_summarize(article, target_word_count=70, iterations=4)
print(f"\nFinal summary (iteration {len(summaries)}):")
print(summaries[-1].summary)
print(f"\nDensity progression:")
for s in summaries:
    print(f"  Iter {s.iteration}: {s.word_count} words | entities added: {s.new_entities}")

# Expected Token Savings: ~40% downstream — denser summaries reduce context in subsequent LLM calls
# Environment: Document Q&A, news summarization, knowledge base compression
```

---

## Option 2: Adaptive CoD — Stop When Density Plateaus

```python
import anthropic
import re
from dataclasses import dataclass

@dataclass
class CoDResult:
    summary: str
    iteration: int
    entities_per_100_words: float
    converged: bool

def count_named_entities(text: str) -> int:
    """Heuristic entity count: capitalized multi-word phrases and numbers."""
    capitalized = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)
    numbers = re.findall(r'\b\d+(?:[.,]\d+)?(?:\s*(?:million|billion|km|kg|°C|%))?\b', text)
    return len(set(capitalized)) + len(numbers)

def adaptive_chain_of_density(
    text: str,
    target_words: int = 80,
    max_iterations: int = 6,
    plateau_threshold: float = 0.5  # Stop if entity density increases < this per iteration
) -> CoDResult:
    client = anthropic.Anthropic()

    prev_density = 0.0
    current_summary = ""

    for i in range(1, max_iterations + 1):
        if i == 1:
            prompt = f"Summarize in ~{target_words} words:\n\n{text}"
            system = "Write a concise informative summary."
        else:
            prompt = f"""Article: {text}

Current summary: {current_summary}

Rewrite to include 2–3 more specific facts/entities from the article.
Keep length at ~{target_words} words by being more concise.
Output ONLY the new summary, nothing else."""
            system = "You are a summarization refiner. Output only the summary text."

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        current_summary = response.content[0].text.strip()
        word_count = len(current_summary.split())
        entity_count = count_named_entities(current_summary)
        density = (entity_count / word_count) * 100

        density_gain = density - prev_density
        print(f"Iter {i}: {word_count}w, {entity_count} entities, density={density:.1f}/100w, "
              f"gain={density_gain:+.1f}")

        # Convergence check: stop if density gain drops below threshold
        if i > 2 and density_gain < plateau_threshold:
            print(f"  -> Converged at iteration {i} (gain {density_gain:.2f} < {plateau_threshold})")
            return CoDResult(current_summary, i, density, converged=True)

        prev_density = density

    return CoDResult(current_summary, max_iterations, prev_density, converged=False)

article = """
OpenAI released GPT-4 on March 14, 2023, marking a significant milestone in AI development.
The model demonstrated multimodal capabilities, accepting both image and text inputs while
producing text outputs. GPT-4 scored in the 90th percentile on the bar exam and achieved
a 1410 SAT score. The model uses RLHF (Reinforcement Learning from Human Feedback) and
Constitutional AI principles. Sam Altman, CEO of OpenAI, noted that GPT-4 is "still flawed"
but represents a major leap from GPT-3.5. Microsoft integrated GPT-4 into Bing Chat and
GitHub Copilot X. The model has a 32,768 token context window in its extended version.
OpenAI partnered with Be My Eyes to help visually impaired users through GPT-4's vision.
"""

result = adaptive_chain_of_density(article, target_words=60, max_iterations=6)
print(f"\nFinal ({result.iteration} iterations, converged={result.converged}):")
print(result.summary)
print(f"Density: {result.entities_per_100_words:.1f} entities/100 words")

# Expected Token Savings: ~30% — stops early when adding iterations no longer improves density
# Environment: Batch summarization pipelines where token budget is constrained
```

---

## Option 3: CoD for Long Documents via Sliding Window

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class ChunkSummary:
    chunk_id: int
    original_words: int
    summary: str
    summary_words: int
    density_iterations: int

def chunk_text(text: str, chunk_words: int = 400) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_words):
        chunk = " ".join(words[i:i + chunk_words])
        chunks.append(chunk)
    return chunks

def cod_summarize_chunk(
    client: anthropic.Anthropic,
    chunk: str,
    target_words: int = 60,
    iterations: int = 3
) -> str:
    """Apply CoD to a single chunk."""
    summary = ""

    for i in range(iterations):
        if i == 0:
            prompt = f"Summarize in ~{target_words} words:\n{chunk}"
            system = "Write a concise factual summary."
        else:
            prompt = f"""Source: {chunk}

Current summary: {summary}

Add 2 more specific details from the source WITHOUT increasing word count (~{target_words} words).
Output ONLY the updated summary."""
            system = "Refine the summary to be denser. Output only the summary."

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.content[0].text.strip()

    return summary

def cod_merge_summaries(
    client: anthropic.Anthropic,
    chunk_summaries: list[str],
    final_word_target: int = 150
) -> str:
    """Merge chunk summaries into a coherent final summary using CoD."""
    combined = "\n\n".join(f"[Part {i+1}]: {s}" for i, s in enumerate(chunk_summaries))

    # Single merge pass — the chunk summaries are already dense
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Merge these section summaries into one coherent ~{final_word_target}-word summary. Preserve all key entities. Eliminate repetition.",
        messages=[{"role": "user", "content": combined}]
    )
    return response.content[0].text.strip()

def cod_long_document(text: str, chunk_words: int = 300, target_compression: float = 0.15):
    client = anthropic.Anthropic()
    chunks = chunk_text(text, chunk_words)
    total_words = len(text.split())
    target_final_words = int(total_words * target_compression)
    words_per_chunk_summary = max(40, target_final_words // len(chunks))

    print(f"Document: {total_words} words, {len(chunks)} chunks")
    print(f"Target: {target_final_words} words ({target_compression:.0%} compression)\n")

    chunk_summaries_text = []
    for i, chunk in enumerate(chunks):
        print(f"Chunk {i+1}/{len(chunks)} ({len(chunk.split())} words)...")
        summary = cod_summarize_chunk(client, chunk, target_words=words_per_chunk_summary, iterations=3)
        chunk_summaries_text.append(summary)
        print(f"  -> {len(summary.split())} words: {summary[:60]}...")

    print(f"\nMerging {len(chunks)} chunk summaries...")
    final = cod_merge_summaries(client, chunk_summaries_text, target_final_words)
    print(f"Final: {len(final.split())} words")
    return final

# Simulated long document
long_doc = """
Artificial intelligence has undergone remarkable transformation since Alan Turing proposed
the Turing Test in 1950. Early AI research focused on symbolic reasoning and rule-based
systems, exemplified by the General Problem Solver developed by Allen Newell and Herbert
Simon in 1957. The first AI winter (1974-1980) struck when funding dried up due to
unmet expectations. Expert systems like MYCIN (1972) briefly revived optimism before
the second AI winter (1987-1993).

The deep learning revolution began with Geoffrey Hinton's backpropagation work in 1986
and accelerated with the AlexNet breakthrough in 2012, where convolutional neural networks
achieved 15.3% top-5 error rate on ImageNet, half the previous record. Google's DeepMind
achieved superhuman Go performance with AlphaGo in 2016, defeating world champion Lee Sedol
4-1. The attention mechanism, introduced in "Attention Is All You Need" by Vaswani et al.
(2017), enabled the Transformer architecture that underpins all modern large language models.

OpenAI's GPT series (2018-2023) scaled language models to 175 billion parameters in GPT-3,
demonstrating emergent capabilities including few-shot learning and code generation.
DALL-E (2021) and Stable Diffusion (2022) democratized image generation. Claude, Anthropic's
AI assistant launched in 2023, introduced Constitutional AI for safer outputs. The 2024
Nobel Prize in Physics recognized Hinton and Hopfield for foundational neural network work.

Current challenges include hallucination rates averaging 3-7% in production systems,
alignment with human values (the RLHF approach by Ziegler et al., 2019), energy consumption
(GPT-3 training used 1,287 MWh), and bias mitigation. Regulatory responses include the EU
AI Act (2024) and US Executive Order on AI (October 2023). Investment reached $91.9 billion
globally in 2023, with China accounting for 15% of total AI research output.
""" * 2  # Double to make it longer

final_summary = cod_long_document(long_doc, chunk_words=250, target_compression=0.12)
print(f"\nFinal summary:\n{final_summary}")

# Expected Token Savings: ~85% — long doc compressed to 12-15% while retaining key entities
# Environment: RAG preprocessing, knowledge base indexing, document intelligence pipelines
```

---

## Option 4: CoD with Quality Scoring and Rollback

```python
import anthropic
import re
from dataclasses import dataclass

@dataclass
class ScoredSummary:
    text: str
    iteration: int
    entity_coverage: float  # 0.0-1.0
    fluency_score: float    # 0.0-1.0
    word_count: int
    composite_score: float

def extract_entities_from_source(text: str) -> set[str]:
    """Extract named entities from source text using simple heuristics."""
    # Capitalized phrases, years, measurements
    entities = set()
    for match in re.finditer(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text):
        entities.add(match.group())
    for match in re.finditer(r'\b\d{4}\b', text):  # Years
        entities.add(match.group())
    for match in re.finditer(r'\b\d+(?:\.\d+)?(?:\s*(?:million|billion|%|km|kg))\b', text, re.IGNORECASE):
        entities.add(match.group().lower())
    return entities

def score_summary(
    client: anthropic.Anthropic,
    summary: str,
    source_entities: set[str],
    iteration: int
) -> ScoredSummary:
    """Score a summary on entity coverage and fluency."""
    summary_lower = summary.lower()
    # Entity coverage: fraction of source entities mentioned in summary
    covered = sum(1 for e in source_entities if e.lower() in summary_lower)
    entity_coverage = covered / len(source_entities) if source_entities else 0.0

    # Fluency: avg sentence length in words (heuristic — too long or too short = bad)
    sentences = [s.strip() for s in re.split(r'[.!?]+', summary) if s.strip()]
    if sentences:
        avg_len = sum(len(s.split()) for s in sentences) / len(sentences)
        # Ideal sentence length is 10-20 words
        fluency_score = max(0.0, 1.0 - abs(avg_len - 15) / 15)
    else:
        fluency_score = 0.0

    word_count = len(summary.split())
    composite = 0.7 * entity_coverage + 0.3 * fluency_score

    return ScoredSummary(
        text=summary,
        iteration=iteration,
        entity_coverage=entity_coverage,
        fluency_score=fluency_score,
        word_count=word_count,
        composite_score=composite
    )

def cod_with_rollback(
    text: str,
    target_words: int = 80,
    iterations: int = 5
) -> ScoredSummary:
    """Run CoD but rollback if an iteration produces a worse summary."""
    client = anthropic.Anthropic()
    source_entities = extract_entities_from_source(text)
    print(f"Source entities: {len(source_entities)} found")

    best: ScoredSummary = None
    current_summary = ""

    for i in range(1, iterations + 1):
        if i == 1:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=f"Summarize in ~{target_words} words. Include specific names, dates, and numbers.",
                messages=[{"role": "user", "content": text}]
            )
        else:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=f"Refine summary to include more specific entities from the source. Keep ~{target_words} words. Output ONLY the summary.",
                messages=[{"role": "user", "content": f"Source:\n{text}\n\nCurrent summary:\n{current_summary}"}]
            )

        candidate = response.content[0].text.strip()
        scored = score_summary(client, candidate, source_entities, i)
        print(f"Iter {i}: {scored.word_count}w, coverage={scored.entity_coverage:.0%}, "
              f"fluency={scored.fluency_score:.2f}, composite={scored.composite_score:.2f}", end="")

        if best is None or scored.composite_score > best.composite_score:
            best = scored
            current_summary = candidate
            print(" ✓ (new best)")
        else:
            print(f" ✗ (rollback to iter {best.iteration})")
            # Keep current_summary as the best one found so far
            current_summary = best.text

    return best

article = """
SpaceX's Starship, the world's largest and most powerful rocket at 120 meters tall and
5,000 tonnes of thrust, completed its fourth integrated flight test on June 6, 2024.
Both the Super Heavy booster and Starship upper stage successfully reentered and splashed
down in the Gulf of Mexico and Indian Ocean respectively. CEO Elon Musk announced the
rocket uses 33 Raptor engines burning liquid methane (CH4) and liquid oxygen (LOX).
NASA awarded SpaceX a $2.9 billion contract to use Starship as the Human Landing System
for the Artemis III mission, targeting the lunar south pole in 2026. The fully reusable
rocket aims to reduce launch costs to $10/kg to orbit versus $2,700/kg for Saturn V.
"""

best_summary = cod_with_rollback(article, target_words=65, iterations=5)
print(f"\nBest summary (iteration {best_summary.iteration}):")
print(best_summary.text)
print(f"\nFinal scores: coverage={best_summary.entity_coverage:.0%}, "
      f"fluency={best_summary.fluency_score:.2f}, composite={best_summary.composite_score:.2f}")

# Expected Token Savings: ~35% downstream — rollback prevents density-fluency trade-off failures
# Environment: Automated pipelines where summary quality must be verified before downstream use
```

---

## Option 5: CoD for Multi-Document Fusion

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class MultiDocCoD:
    source_count: int
    individual_summaries: list[str]
    fused_summary: str
    iterations_used: int
    final_word_count: int

def cod_individual(client: anthropic.Anthropic, doc: str, target_words: int, iterations: int) -> str:
    """Summarize a single document with CoD."""
    summary = ""
    for i in range(iterations):
        if i == 0:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=f"Summarize in ~{target_words} words. Be specific.",
                messages=[{"role": "user", "content": doc}]
            )
        else:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=f"Add more specific details without exceeding ~{target_words} words. Output ONLY the summary.",
                messages=[{"role": "user", "content": f"Source: {doc}\n\nSummary: {summary}"}]
            )
        summary = response.content[0].text.strip()
    return summary

def cod_fuse_documents(
    client: anthropic.Anthropic,
    summaries: list[str],
    target_words: int = 100,
    fusion_iterations: int = 3
) -> str:
    """Apply CoD to fuse multiple document summaries."""
    combined = "\n\n".join(f"[Source {i+1}]: {s}" for i, s in enumerate(summaries))
    fused = ""

    for i in range(fusion_iterations):
        if i == 0:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=f"Synthesize these sources into one ~{target_words}-word summary. Merge overlapping info. Highlight agreements and conflicts.",
                messages=[{"role": "user", "content": combined}]
            )
        else:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=f"The fused summary is missing some specific facts. Add 2-3 more without exceeding ~{target_words} words. Output ONLY the summary.",
                messages=[{"role": "user", "content": f"Sources:\n{combined}\n\nCurrent fusion:\n{fused}"}]
            )
        fused = response.content[0].text.strip()
        print(f"  Fusion iter {i+1} ({len(fused.split())} words): {fused[:60]}...")

    return fused

def multi_document_cod(
    documents: list[str],
    words_per_doc_summary: int = 50,
    final_target_words: int = 120,
    doc_iterations: int = 2,
    fusion_iterations: int = 3
) -> MultiDocCoD:
    client = anthropic.Anthropic()
    print(f"Processing {len(documents)} documents with CoD...\n")

    individual_summaries = []
    for i, doc in enumerate(documents):
        print(f"Document {i+1} ({len(doc.split())} words):")
        summary = cod_individual(client, doc, words_per_doc_summary, doc_iterations)
        individual_summaries.append(summary)
        print(f"  Summary ({len(summary.split())} words): {summary[:60]}...\n")

    print(f"Fusing {len(documents)} summaries into final {final_target_words}-word output:")
    fused = cod_fuse_documents(client, individual_summaries, final_target_words, fusion_iterations)

    return MultiDocCoD(
        source_count=len(documents),
        individual_summaries=individual_summaries,
        fused_summary=fused,
        iterations_used=doc_iterations + fusion_iterations,
        final_word_count=len(fused.split())
    )

# Simulate multiple news articles on the same topic
docs = [
    """Reuters: The Federal Reserve held interest rates steady at 5.25-5.50% on Wednesday,
    citing persistent inflation concerns. Fed Chair Jerome Powell stated the central bank
    needs 'greater confidence' that inflation is sustainably moving toward the 2% target
    before cutting rates. Core PCE inflation stood at 2.6% in March 2024. Markets had
    priced in 6 rate cuts for 2024 at the year's start but now expect only 1-2.""",

    """Bloomberg: Federal Reserve officials voted unanimously to maintain the federal funds
    rate at its 23-year high of 5.25-5.50%. The FOMC statement noted strong job market
    data with unemployment at 3.7% complicates the path to rate cuts. Powell emphasized
    'restrictive policy is needed for longer' after Q1 GDP growth disappointed at 1.6%
    annualized. Treasury yields rose 8 basis points following the decision.""",

    """WSJ: The Fed's May 2024 meeting produced no surprises as policymakers left rates
    unchanged for the sixth consecutive meeting. Minutes showed disagreement on the pace
    of future cuts, with some members arguing 1 cut in 2024 is appropriate while others
    prefer waiting until 2025. Balance sheet runoff (QT) continues at $60 billion monthly.
    The next rate decision is June 12, when updated dot plots will be released.""",
]

result = multi_document_cod(docs, words_per_doc_summary=45, final_target_words=100)
print(f"\nFinal multi-document fusion ({result.final_word_count} words):")
print(result.fused_summary)

# Expected Token Savings: ~70% downstream — 3 long articles compressed into 1 dense 100-word synthesis
# Environment: News aggregation, research synthesis, competitive intelligence pipelines
```

---

## Option 6: CoD Pipeline with Token-Budget Awareness

```python
import anthropic
from dataclasses import dataclass

@dataclass
class BudgetedCoD:
    summary: str
    word_count: int
    iterations_run: int
    input_tokens_used: int
    budget_remaining: int

def cod_with_token_budget(
    text: str,
    target_words: int = 80,
    token_budget: int = 3000,
    max_iterations: int = 5
) -> BudgetedCoD:
    """Run CoD iterations until the target word count or token budget is reached."""
    client = anthropic.Anthropic()
    tokens_used = 0
    summary = ""

    for i in range(max_iterations):
        # Estimate cost of next iteration: input tokens ≈ len(text.split())*1.3 + len(summary.split())*1.3
        estimated_input_tokens = int(len(text.split()) * 1.3 + len(summary.split()) * 1.3 + 100)
        if tokens_used + estimated_input_tokens > token_budget:
            print(f"  Budget limit: stopping at iteration {i} (used {tokens_used}/{token_budget} tokens)")
            break

        if i == 0:
            system = f"Summarize in ~{target_words} words with specific facts, names, and numbers."
            user_msg = text
        else:
            system = f"Enrich the summary with 2-3 more specific facts from the source. Keep ~{target_words} words. Output ONLY the summary."
            user_msg = f"Source:\n{text}\n\nCurrent summary:\n{summary}"

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}]
        )
        summary = response.content[0].text.strip()
        iter_tokens = response.usage.input_tokens + response.usage.output_tokens
        tokens_used += iter_tokens

        word_count = len(summary.split())
        print(f"  Iter {i+1}: {word_count}w, {iter_tokens} tokens (total: {tokens_used}/{token_budget})")
        print(f"    {summary[:80]}...")

        # Check if we've reached a satisfactory density (word count close to target)
        if abs(word_count - target_words) <= 10 and i >= 2:
            print(f"  Density target reached at iteration {i+1}")
            break

    return BudgetedCoD(
        summary=summary,
        word_count=len(summary.split()),
        iterations_run=i + 1,
        input_tokens_used=tokens_used,
        budget_remaining=token_budget - tokens_used
    )

def run_batch_cod_with_budget(documents: list[dict], total_token_budget: int = 15000):
    """Process multiple documents sharing a token budget."""
    remaining_budget = total_token_budget
    results = []

    for doc in documents:
        if remaining_budget < 500:
            print(f"\n[Budget exhausted] Skipping: {doc['title']}")
            continue

        per_doc_budget = min(remaining_budget // max(1, len(documents) - len(results)), 3000)
        print(f"\n{doc['title']} (budget: {per_doc_budget} tokens):")
        result = cod_with_token_budget(
            doc["text"],
            target_words=doc.get("target_words", 60),
            token_budget=per_doc_budget
        )
        remaining_budget -= result.input_tokens_used
        results.append({"title": doc["title"], "result": result})
        print(f"  Done: {result.word_count}w, {result.iterations_run} iters, "
              f"{result.input_tokens_used} tokens | budget left: {remaining_budget}")

    print(f"\nBatch summary:")
    print(f"  Documents processed: {len(results)}/{len(documents)}")
    print(f"  Total tokens used: {total_token_budget - remaining_budget}/{total_token_budget}")
    for r in results:
        print(f"\n  [{r['title']}] ({r['result'].word_count} words):")
        print(f"    {r['result'].summary[:120]}...")

documents = [
    {
        "title": "AI Chip Market",
        "target_words": 60,
        "text": "NVIDIA's H100 GPU, priced at $30,000-$40,000 per unit, dominates the AI training market with 70-95% share. AMD's MI300X competitor launched in December 2023 offers 192GB HBM3 memory vs H100's 80GB. Intel's Gaudi 3 targets inference workloads at lower cost. Global AI chip market projected to reach $309 billion by 2029. TSMC manufactures both NVIDIA and AMD chips using 5nm process. Google's TPU v5, AWS Trainium2, and Microsoft's Maia 100 represent hyperscaler alternatives to NVIDIA dependency."
    },
    {
        "title": "Quantum Computing Progress",
        "target_words": 60,
        "text": "IBM unveiled its 1,121-qubit Condor processor in December 2023, the largest superconducting quantum chip yet. Google's Sycamore achieved quantum supremacy in 2019 with 53 qubits. Microsoft's topological qubit approach aims for error rates below 1 in 1 trillion. IonQ uses trapped-ion technology with 35 algorithmic qubits. Quantum volume, a benchmarking metric, measures overall system performance. DARPA's Underexplored Systems for Utility-Scale Quantum Computing program awarded $45M in 2024. Error correction remains the key obstacle to fault-tolerant quantum computing."
    },
]

run_batch_cod_with_budget(documents, total_token_budget=12000)

# Expected Token Savings: ~50% overall — budget-aware CoD maximizes density within cost constraints
# Environment: Production batch pipelines with strict token cost budgets across many documents
```

---

## Comparison

| Option | Iterations | Stops Early | Handles Long Docs | Multi-Doc | Budget-Aware | Best For |
|--------|-----------|------------|------------------|-----------|-------------|----------|
| Fixed Iterations | Fixed N | No | No | No | No | Baseline CoD for short documents |
| Adaptive CoD | Variable | Yes (plateau) | No | No | No | Cost-sensitive, stops when density converges |
| Sliding Window | Fixed N | No | Yes | No | No | Long document compression |
| Quality + Rollback | Fixed N | No | No | No | No | Quality-critical pipelines needing best-of-N |
| Multi-Doc Fusion | Fixed N | No | No | Yes | No | News synthesis, research aggregation |
| Budget-Aware | Variable | Yes (budget) | No | Batch | Yes | Production batch with token cost limits |

**Recommendation:** Start with **Option 1** (fixed iterations, 3–4 rounds) for most summarization tasks — it reliably improves information density with minimal complexity. Use **Option 2** (adaptive) to save tokens on documents that converge quickly. Use **Option 3** (sliding window) for documents longer than your context window. Apply **Option 6** (budget-aware) in production batch pipelines where token costs are directly tracked.
