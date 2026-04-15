---
layout: solution
title: "Agent Cites Sources That Don't Exist"
category: hallucination
description: "Agent invents plausible-sounding paper titles, URLs, and author names when asked to provide references, producing confident fabricated citations."
tags: [hallucination, citations, grounding, retrieval, reliability]
---

## Symptom

Users ask for sources to support the agent's claims. The agent responds with specific paper titles, journal names, DOIs, and author names — all of which turn out to be fabricated. The citations look legitimate but return 404s or don't appear in any academic database. Users who don't verify sources propagate the misinformation.

## Root Cause

Language models do not retrieve from the internet during generation. When asked for citations, they pattern-match on what a citation *looks like* from training data and generate plausible-sounding strings. The model has no mechanism to verify that a generated citation corresponds to a real document. The more authoritative the request ("cite a peer-reviewed study"), the more confidently plausible (and fabricated) the output.

## Fix

### Option 1 — System prompt: never fabricate, acknowledge limitations

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a research assistant with comprehensive knowledge up to your training cutoff.

Citation policy (strictly enforced):
- ONLY cite sources you are highly confident exist and whose content you accurately recall.
- If you are not certain a source exists, do NOT cite it.
- When you cannot cite a source, say explicitly: "I cannot verify a specific source for this claim."
- Suggest where the user could search (Google Scholar, PubMed, arXiv) rather than inventing citations.
- Never generate DOIs, ISBNs, or URLs for citations — they are too easy to fabricate incorrectly."""

def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# Questions that would normally trigger citation hallucination
print(ask("What are the key studies showing that sleep deprivation affects memory?"))
print("---")
print(ask("Cite the paper that introduced the transformer architecture."))
print("---")
print(ask("What research supports intermittent fasting for weight loss?"))
```

**Expected Token Savings:** Prevents correction turns triggered by users discovering fabricated sources; a clear "I cannot verify" is better than a plausible lie.
**Environment:** All research-adjacent agents; this system prompt is the mandatory baseline.

---

### Option 2 — Retrieve-then-cite: only cite documents in the retrieved context

```python
import anthropic

client = anthropic.Anthropic()

# Simulated document store — in production: vector DB, search API, or knowledge base
DOCUMENT_STORE = [
    {
        "id": "doc-001",
        "title": "Attention Is All You Need",
        "authors": "Vaswani et al.",
        "year": 2017,
        "venue": "NeurIPS",
        "abstract": "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.",
    },
    {
        "id": "doc-002",
        "title": "BERT: Pre-training of Deep Bidirectional Transformers",
        "authors": "Devlin et al.",
        "year": 2019,
        "venue": "NAACL",
        "abstract": "We introduce BERT, which stands for Bidirectional Encoder Representations from Transformers.",
    },
    {
        "id": "doc-003",
        "title": "Language Models are Few-Shot Learners",
        "authors": "Brown et al.",
        "year": 2020,
        "venue": "NeurIPS",
        "abstract": "We show that scaling language models greatly improves task-agnostic, few-shot performance.",
    },
]

def retrieve(query: str, top_k: int = 2) -> list[dict]:
    """Keyword search — replace with embedding similarity in production."""
    query_words = set(query.lower().split())
    scored = []
    for doc in DOCUMENT_STORE:
        doc_text = f"{doc['title']} {doc['abstract']}".lower()
        score    = sum(1 for w in query_words if w in doc_text)
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]

def ask_with_retrieval(question: str) -> str:
    docs = retrieve(question)
    if not docs:
        context = "No relevant documents found in the knowledge base."
    else:
        context = "\n\n".join(
            f"[{doc['id']}] {doc['title']} ({doc['authors']}, {doc['year']})\n{doc['abstract']}"
            for doc in docs
        )

    system = f"""Answer the user's question using ONLY the documents provided below.
If citing a source, use its ID (e.g., [doc-001]). Do not cite anything not in the documents.
If the documents don't answer the question, say so.

DOCUMENTS:
{context}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

print(ask_with_retrieval("What paper introduced the Transformer architecture?"))
print("---")
print(ask_with_retrieval("What is GPT-4?"))  # not in store — should admit it
```

**Expected Token Savings:** Grounded citations eliminate fabrication entirely; the model can only cite what it was given.
**Environment:** RAG pipelines, knowledge base Q&A, document summarisation; the gold standard for citation accuracy.

---

### Option 3 — Citation verification via web search

```python
import json
import anthropic

client = anthropic.Anthropic()

# Simulated web search (replace with real search API: Tavily, Bing, etc.)
KNOWN_PAPERS = {
    "attention is all you need":            {"verified": True,  "url": "https://arxiv.org/abs/1706.03762"},
    "bert pre-training deep bidirectional": {"verified": True,  "url": "https://arxiv.org/abs/1810.04805"},
    "gpt-4 technical report":               {"verified": True,  "url": "https://arxiv.org/abs/2303.08774"},
}

def verify_citation(title: str) -> dict:
    """Check if a cited paper title can be verified."""
    normalised = title.lower().strip()
    for key, data in KNOWN_PAPERS.items():
        if key in normalised or normalised in key:
            return {"verified": True, "url": data["url"], "title": title}
    return {"verified": False, "url": None, "title": title}

EXTRACT_SYSTEM = """Extract any citations or sources mentioned in the text.
Return a JSON array: [{"title": "...", "authors": "...", "year": "..."}]
If no citations, return []."""

def extract_citations(text: str) -> list[dict]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    try:
        raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
        return json.loads(raw)
    except json.JSONDecodeError:
        return []

def ask_and_verify(question: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    answer = response.content[0].text

    citations  = extract_citations(answer)
    verified   = [verify_citation(c.get("title", "")) for c in citations]
    unverified = [v for v in verified if not v["verified"]]

    if unverified:
        print(f"[verify] {len(unverified)} unverified citation(s): {[v['title'] for v in unverified]}")
        # Append a warning to the response
        answer += f"\n\n⚠️ Note: {len(unverified)} citation(s) could not be verified and may be inaccurate."

    return {"answer": answer, "verified": [v for v in verified if v["verified"]], "unverified": unverified}

result = ask_and_verify("What papers introduced the Transformer and BERT?")
print(result["answer"][:500])
print(f"\nVerified: {len(result['verified'])} | Unverified: {len(result['unverified'])}")
```

**Expected Token Savings:** Verification catches fabrications before they reach users; warning flag reduces downstream trust damage.
**Environment:** Research assistants with access to a search API; adds one cheap verification pass after generation.

---

### Option 4 — Structured citation format that forces acknowledgment of uncertainty

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """When citing sources, you MUST use this exact JSON structure for each citation:
{
  "claim": "the specific claim this citation supports",
  "source_type": "peer_reviewed_paper" | "textbook" | "known_standard" | "general_knowledge",
  "title": "exact title if known, else null",
  "authors": "authors if known, else null",
  "year": "year if known, else null",
  "confidence": "high" | "medium" | "low",
  "note": "any uncertainty about this citation"
}

For source_type "general_knowledge": omit title/authors/year — these facts don't need citations.
Set confidence to "low" if you are not certain this source exists.
Never set confidence to "high" for a source you cannot precisely recall."""

def ask_with_structured_citations(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"{question}\n\nProvide your answer, then list any citations as JSON objects.",
        }],
    )
    return response.content[0].text

answer = ask_with_structured_citations(
    "What evidence supports the use of spaced repetition for long-term memory retention?"
)
print(answer[:800])
```

**Expected Token Savings:** Structured format forces the model to express uncertainty explicitly; "low confidence" citations are filtered or flagged by downstream code.
**Environment:** Research tools where citation quality metadata is needed; pairs with a post-processing step that filters low-confidence citations.

---

### Option 5 — Claim-first approach: generate claims, then find evidence separately

```python
import anthropic

client = anthropic.Anthropic()

CLAIM_SYSTEM = """Extract the key factual claims from the answer.
Return a numbered list of specific, verifiable claims. No citations yet."""

EVIDENCE_SYSTEM = """For each claim, indicate whether you can provide a HIGH-CONFIDENCE citation
(a source you are certain exists and accurately represents).
If not, recommend a search query the user can run.

Format:
Claim 1: [claim text]
Citation: [title, authors, year] OR Search: [suggested query on Google Scholar/PubMed]
Confidence: high | medium | low"""

def two_stage_research(question: str) -> str:
    # Stage 1: generate the answer with claims
    response1 = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    answer = response1.content[0].text

    # Stage 2: source the claims separately
    response2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=EVIDENCE_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Answer:\n{answer}\n\nNow provide evidence or search recommendations for each claim.",
        }],
    )
    evidence = response2.content[0].text

    return f"=== ANSWER ===\n{answer}\n\n=== EVIDENCE ===\n{evidence}"

result = two_stage_research("How does caffeine affect adenosine receptors?")
print(result[:800])
```

**Expected Token Savings:** Separating claim generation from citation generation reduces fabrication; the model in stage 2 is focused only on what it *knows*, not on sounding authoritative.
**Environment:** Research assistants; two-stage approach is more reliable than asking for citations inline.

---

### Option 6 — Citation sandbox: validate DOIs and URLs before returning

```python
import re
import urllib.request
import anthropic

client = anthropic.Anthropic()

DOI_PATTERN  = re.compile(r"10\.\d{4,}/[^\s,]+")
URL_PATTERN  = re.compile(r"https?://[^\s,)]+")
ARXIV_PATTERN = re.compile(r"arxiv\.org/abs/(\d{4}\.\d{4,})")

def validate_doi(doi: str) -> bool:
    """Check if DOI resolves (simplified — use doi.org API in production)."""
    known_valid = {"10.48550/arXiv.1706.03762", "10.18653/v1/N19-1423"}
    return doi in known_valid

def validate_url(url: str) -> bool:
    """HEAD request to check if URL exists."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0")
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False

def scan_and_validate(text: str) -> dict:
    dois = DOI_PATTERN.findall(text)
    urls = URL_PATTERN.findall(text)

    results = {
        "dois":          {},
        "urls":          {},
        "invalid_count": 0,
    }

    for doi in dois:
        valid = validate_doi(doi)
        results["dois"][doi] = valid
        if not valid:
            results["invalid_count"] += 1
            print(f"[citation] INVALID DOI: {doi}")

    for url in urls[:3]:  # limit URL checks to avoid rate limits
        if "doi.org" in url or "arxiv.org" in url:
            valid = validate_url(url)
            results["urls"][url] = valid
            if not valid:
                results["invalid_count"] += 1
                print(f"[citation] INVALID URL: {url}")

    return results

def ask_and_validate(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    text    = response.content[0].text
    results = scan_and_validate(text)

    if results["invalid_count"] > 0:
        text += f"\n\n⚠️ {results['invalid_count']} citation(s) in this response could not be verified."

    return text

print(ask_and_validate("What is the DOI for 'Attention Is All You Need'?"))
```

**Expected Token Savings:** URL/DOI validation is a network call, not an API call — cheap; each caught fabrication saves the user from propagating false information.
**Environment:** High-trust research tools where citations are acted upon (legal, medical, academic); add as a post-processing filter.

---

## Comparison

| Option | Fabrication Prevention | Requires External API | Latency | Best For |
|---|---|---|---|---|
| 1. System prompt | Partial (model still may) | No | None | Baseline — always apply |
| 2. Retrieve-then-cite | 100% (grounded) | No (local DB) | Low | RAG pipelines, knowledge bases |
| 3. Web verification | Post-generation check | Yes (search) | +1 call | Research assistants with search access |
| 4. Structured format | Partial (forces confidence) | No | None | Uncertainty-aware research tools |
| 5. Two-stage claim/evidence | High (separated concerns) | No | +1 call | General research Q&A |
| 6. DOI/URL validation | Post-generation validation | Yes (HTTP) | +HEAD calls | High-trust legal/medical/academic |
