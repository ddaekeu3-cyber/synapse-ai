---
layout: solution
title: "Agent Fabricates Citations and Sources"
category: hallucination
description: "Agent invents URLs, paper titles, author names, and publication dates that appear authoritative but do not exist."
tags: [hallucination, citations, grounding, tool-use, retrieval]
---

## Symptom

The agent responds with convincing-looking citations that cannot be verified:

```
"According to a 2023 study by Dr. Sarah Chen et al. published in Nature Machine Intelligence
(https://nature.com/articles/nmi-2023-4521), transformer architectures show..."

# That URL doesn't exist. The paper doesn't exist. Dr. Chen may or may not exist.
```

Fabricated citations are especially dangerous because:
- They look authoritative and are rarely checked
- Users copy them into documents, reports, and academic work
- The citation format (journal, volume, page) is structurally correct even when the content is invented
- The model's training data contains millions of real citations, making invented ones indistinguishable in tone

---

## Root Cause

Language models learn citation patterns deeply — they can generate plausible-sounding author names, journal titles, DOIs, and URLs without any grounding in real sources. When asked to "cite sources" without being given actual sources, the model produces citations using the same next-token prediction that produces everything else: statistically plausible but factually unconstrained.

Three structural mitigations work: (1) never ask the agent to cite sources it doesn't have access to, (2) force citations to come from retrieved documents, (3) validate URLs and DOIs before including them.

---

## Fix

### Option 1 — Ground All Citations in Retrieved Documents

Provide source documents as context and instruct the agent to cite only from that context.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Documents retrieved from a real source (e.g., web search, vector DB, knowledge base)
def retrieve_documents(query: str) -> list[dict]:
    """
    In production: call a real retrieval system.
    Returns documents with title, url, content, and retrieval_date.
    """
    # Simulated retrieved documents — in real use, these come from search/RAG
    return [
        {
            "id": "doc1",
            "title": "Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762",
            "authors": "Vaswani et al.",
            "year": 2017,
            "excerpt": (
                "We propose a new simple network architecture, the Transformer, "
                "based solely on attention mechanisms, dispensing with recurrence and convolutions entirely."
            ),
            "retrieval_date": "2026-04-15",
        },
        {
            "id": "doc2",
            "title": "BERT: Pre-training of Deep Bidirectional Transformers",
            "url": "https://arxiv.org/abs/1810.04805",
            "authors": "Devlin et al.",
            "year": 2018,
            "excerpt": (
                "We introduce a new language representation model called BERT, which stands for "
                "Bidirectional Encoder Representations from Transformers."
            ),
            "retrieval_date": "2026-04-15",
        },
    ]

def build_grounded_system(documents: list[dict]) -> str:
    doc_block = "\n\n".join(
        f"[{doc['id']}] {doc['title']} ({doc['authors']}, {doc['year']})\n"
        f"URL: {doc['url']}\n"
        f"Excerpt: {doc['excerpt']}"
        for doc in documents
    )

    return f"""You are a research assistant. Answer questions using ONLY the provided documents.

## Source Documents
{doc_block}

## Citation Rules (STRICT)
1. You may ONLY cite documents listed above using their [id]
2. Format citations as: ([id]: Title, Author, Year)
3. If you cannot find an answer in the provided documents, say:
   "I don't have a source for that in my current documents. I can search for more."
4. NEVER invent citations, URLs, author names, or paper titles
5. NEVER cite memory or training knowledge as a source
"""

def answer_with_grounded_citations(query: str) -> dict:
    docs = retrieve_documents(query)
    system = build_grounded_system(docs)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": query}]
    )

    reply = response.content[0].text

    # Verify all cited IDs are real
    doc_ids = {doc["id"] for doc in docs}
    import re
    cited_ids = re.findall(r'\[(\w+)\]', reply)
    invalid_citations = [cid for cid in cited_ids if cid not in doc_ids]

    return {
        "answer": reply,
        "sources_available": [d["id"] for d in docs],
        "sources_cited": cited_ids,
        "invalid_citations": invalid_citations,
        "citation_integrity": len(invalid_citations) == 0,
    }

result = answer_with_grounded_citations(
    "How do transformer architectures work and what are their key innovations?"
)
print(result["answer"])
print(f"\nCitation integrity: {result['citation_integrity']}")
if result["invalid_citations"]:
    print(f"WARNING: Unchecked citations: {result['invalid_citations']}")
```

**Expected Token Savings:** No savings — correctness fix. Prevents reputational damage from fabricated citations distributed to users.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 2 — Citation Tool with URL Validation

Force all citations through a tool call — the tool validates the URL before the agent can use it.

```python
import anthropic
import urllib.request
import urllib.error
import json
import re
from typing import Optional

client = anthropic.Anthropic()

def validate_url_exists(url: str, timeout: int = 5) -> tuple[bool, str]:
    """Check if a URL returns a non-error response."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            return status < 400, f"HTTP {status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:100]

def validate_doi(doi: str) -> tuple[bool, str]:
    """Validate a DOI exists via doi.org resolution."""
    doi_clean = doi.strip().lstrip("https://doi.org/").lstrip("doi:")
    url = f"https://doi.org/{doi_clean}"
    return validate_url_exists(url)

CITATION_TOOL = {
    "name": "submit_citation",
    "description": (
        "Submit a citation to be validated and included in your response. "
        "All citations MUST go through this tool — never write citation text directly. "
        "The tool will verify the source exists before including it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Full title of the work"},
            "authors": {"type": "string", "description": "Author(s) — last name, first initial"},
            "year": {"type": "integer", "description": "Publication year"},
            "url": {"type": "string", "description": "Direct URL to the source"},
            "doi": {"type": "string", "description": "DOI if available (optional)"},
            "relevant_quote": {"type": "string", "description": "The specific excerpt that supports your claim"},
        },
        "required": ["title", "authors", "year", "url", "relevant_quote"]
    }
}

class CitationRegistry:
    """Validates and stores citations during a response generation."""

    def __init__(self):
        self.valid: list[dict] = []
        self.rejected: list[dict] = []

    def submit(self, citation: dict) -> str:
        url = citation.get("url", "")
        doi = citation.get("doi", "")

        # Validate URL
        url_valid, url_status = validate_url_exists(url) if url else (False, "no URL provided")

        # Validate DOI if provided
        doi_valid = True
        if doi:
            doi_valid, doi_status = validate_doi(doi)

        if url_valid:
            self.valid.append(citation)
            return json.dumps({
                "accepted": True,
                "citation_id": f"ref{len(self.valid)}",
                "message": f"Citation validated ({url_status}). Use [ref{len(self.valid)}] to cite it.",
            })
        else:
            self.rejected.append({**citation, "reason": url_status})
            return json.dumps({
                "accepted": False,
                "reason": f"URL validation failed: {url_status}. Do NOT cite this source.",
                "guidance": (
                    "If this is a real source, provide the correct URL. "
                    "If you are unsure of the URL, do not cite it — "
                    "instead say you recall this topic but cannot provide a verified source."
                ),
            })

    def format_bibliography(self) -> str:
        if not self.valid:
            return "No verified sources cited."
        lines = []
        for i, c in enumerate(self.valid, 1):
            lines.append(
                f"[ref{i}] {c['authors']} ({c['year']}). {c['title']}. {c['url']}"
            )
        return "## Sources\n" + "\n".join(lines)

def research_with_validation(query: str) -> str:
    registry = CitationRegistry()

    system = """You are a research assistant. Support every claim with a citation.
ALL citations must be submitted via the submit_citation tool — never write citation text inline.
If a citation is rejected (URL invalid), say "I recall this topic but cannot provide a verified source."
Never invent URLs — only cite sources you are confident exist."""

    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=[CITATION_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            answer = ""
            for block in response.content:
                if hasattr(block, "text"):
                    answer += block.text
            return answer + "\n\n" + registry.format_bibliography()

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_citation":
                result = registry.submit(block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})

answer = research_with_validation("Explain the transformer architecture and cite the original paper.")
print(answer)
print(f"\nValid citations: {len([])}, Rejected: {len([])}")
```

**Expected Token Savings:** Each rejected citation saves ~200 tokens of downstream correction. More importantly, prevents misinformation.

**Environment:** Python 3.9+, `urllib`, `anthropic>=0.40.0`. URL validation adds ~1-5s per citation.

---

### Option 3 — Post-Generation Citation Audit

After generation, extract all citations and verify them before returning to the user.

```python
import anthropic
import re
import urllib.request
import json
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class ExtractedCitation:
    raw_text: str
    url: Optional[str] = None
    doi: Optional[str] = None
    title: Optional[str] = None
    is_verified: Optional[bool] = None
    verification_message: str = ""

def extract_citations(text: str) -> list[ExtractedCitation]:
    """Extract URLs, DOIs, and bracketed citations from response text."""
    citations = []

    # Extract URLs
    for url in re.findall(r'https?://[^\s\)\]\,\"]+', text):
        citations.append(ExtractedCitation(raw_text=url, url=url))

    # Extract DOIs
    for doi in re.findall(r'(?:doi:|https://doi\.org/)[\w./\-]+', text, re.IGNORECASE):
        doi_clean = re.sub(r'^(doi:|https://doi\.org/)', '', doi, flags=re.IGNORECASE)
        citations.append(ExtractedCitation(raw_text=doi, doi=doi_clean))

    return citations

def verify_citation(citation: ExtractedCitation) -> ExtractedCitation:
    """Verify a single citation by checking if the URL/DOI resolves."""
    check_url = citation.url

    if citation.doi and not check_url:
        check_url = f"https://doi.org/{citation.doi}"

    if not check_url:
        citation.is_verified = False
        citation.verification_message = "No URL to verify"
        return citation

    try:
        req = urllib.request.Request(
            check_url, method="HEAD",
            headers={"User-Agent": "CitationValidator/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            citation.is_verified = resp.status < 400
            citation.verification_message = f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        citation.is_verified = False
        citation.verification_message = f"HTTP {e.code} — URL does not exist"
    except Exception as e:
        citation.is_verified = None  # couldn't verify (network error, etc.)
        citation.verification_message = f"Could not verify: {str(e)[:80]}"

    return citation

def generate_and_audit(query: str) -> dict:
    """Generate a response, then audit all citations before returning."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "You are a knowledgeable research assistant. "
            "When citing sources, include full URLs. "
            "Only cite sources you are confident exist."
        ),
        messages=[{"role": "user", "content": query}]
    )

    original_text = response.content[0].text
    citations = extract_citations(original_text)
    verified_citations = [verify_citation(c) for c in citations]

    broken = [c for c in verified_citations if c.is_verified is False]
    verified = [c for c in verified_citations if c.is_verified is True]
    uncertain = [c for c in verified_citations if c.is_verified is None]

    # Add audit warning to response if broken citations found
    final_text = original_text
    if broken:
        warning = (
            f"\n\n⚠️ **Citation Audit Warning**: {len(broken)} citation(s) "
            f"could not be verified:\n"
        )
        for c in broken:
            warning += f"- `{c.raw_text}`: {c.verification_message}\n"
        warning += "Please verify these sources independently before citing them."
        final_text += warning

    return {
        "response": final_text,
        "citations_total": len(citations),
        "citations_verified": len(verified),
        "citations_broken": len(broken),
        "citations_uncertain": len(uncertain),
        "broken_urls": [c.raw_text for c in broken],
        "is_safe": len(broken) == 0,
    }

result = generate_and_audit(
    "What are the key papers on attention mechanisms? Include URLs."
)
print(result["response"][:500])
print(f"\nCitation audit: {result['citations_verified']} valid, {result['citations_broken']} broken")
```

**Expected Token Savings:** Post-generation audit catches fabricated URLs before they reach users, eliminating the need for follow-up corrections.

**Environment:** Python 3.9+, `re`, `urllib`, `anthropic>=0.40.0`.

---

### Option 4 — Explicit No-Citation Mode with Knowledge Disclosure

When no sources are provided, instruct the agent to clearly label knowledge as unverified rather than inventing citations.

```python
import anthropic

client = anthropic.Anthropic()

NO_CITATION_SYSTEM = """You are a knowledgeable assistant.

## Knowledge Disclosure Policy (STRICT)

You have broad training knowledge but NO access to real-time sources, databases, or the internet.

When sharing factual information, ALWAYS use these disclosure formats:

**For well-established facts:**
"[From training] According to widely-cited research, transformers were introduced in 2017..."

**For claims you're uncertain about:**
"[Unverified] I believe this paper exists, but I cannot confirm the URL or exact details..."

**For recent information:**
"[May be outdated] As of my training data, the latest version was X, but this may have changed..."

**NEVER do these things:**
- Invent URLs, DOIs, or paper identifiers — if you don't know the real URL, don't provide one
- Write citations in academic format (Author, Year, Journal) as if from a real source
- Say "according to [paper]" for papers you're not certain exist
- Omit the disclosure label when sharing unverified information

**When asked for sources you cannot verify:**
Say: "I can share what I know from training, but I can't provide a verified citation.
For a reliable source, I'd suggest searching [Google Scholar / PubMed / arxiv.org] for [search terms]."
"""

def honest_knowledge_response(query: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=NO_CITATION_SYSTEM,
        messages=[{"role": "user", "content": query}]
    )
    return response.content[0].text

tests = [
    "What papers introduced the transformer architecture? Please cite them.",
    "Is there research showing that larger models are always better?",
    "What's the latest GPT model and when was it released?",
]

for query in tests:
    print(f"\nQuery: {query}")
    print(f"Response: {honest_knowledge_response(query)[:300]}")
    print()
```

**Expected Token Savings:** No savings — trust fix. Users who receive disclosed-knowledge responses trust the system more and require fewer corrections.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 5 — RAG Pipeline with Citation Enforcement

Full retrieval-augmented generation pipeline that makes fabrication structurally impossible.

```python
import anthropic
import json
from typing import Optional

client = anthropic.Anthropic()

# Simulated vector store — in production: use Pinecone, Weaviate, pgvector, etc.
DOCUMENT_STORE = {
    "doc_vaswani_2017": {
        "title": "Attention Is All You Need",
        "authors": "Vaswani, A., Shazeer, N., Parmar, N., et al.",
        "year": 2017,
        "url": "https://arxiv.org/abs/1706.03762",
        "abstract": "We propose the Transformer architecture based solely on attention mechanisms.",
        "content": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks...",
    },
    "doc_devlin_2018": {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        "authors": "Devlin, J., Chang, M., Lee, K., Toutanova, K.",
        "year": 2018,
        "url": "https://arxiv.org/abs/1810.04805",
        "abstract": "We introduce BERT, a new language representation model.",
        "content": "Unlike recent language representation models, BERT is designed to pre-train deep bidirectional representations...",
    },
}

def retrieve(query: str, top_k: int = 3) -> list[dict]:
    """Simulated retrieval — returns relevant document chunks."""
    # In production: embed query, search vector DB, return top_k results
    # Here: return all docs as simulation
    return [
        {
            "doc_id": doc_id,
            "title": doc["title"],
            "authors": doc["authors"],
            "year": doc["year"],
            "url": doc["url"],
            "excerpt": doc["content"][:200],
        }
        for doc_id, doc in DOCUMENT_STORE.items()
    ][:top_k]

def rag_answer(query: str) -> dict:
    """Answer using RAG — only retrieved documents can be cited."""
    retrieved_docs = retrieve(query)

    # Format context for the model
    context_blocks = []
    for doc in retrieved_docs:
        context_blocks.append(
            f"[{doc['doc_id']}]\n"
            f"Title: {doc['title']}\n"
            f"Authors: {doc['authors']} ({doc['year']})\n"
            f"URL: {doc['url']}\n"
            f"Excerpt: {doc['excerpt']}"
        )
    context = "\n\n---\n\n".join(context_blocks)

    # Build response tool for structured citation
    response_tool = {
        "name": "write_response",
        "description": "Write a response with grounded citations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The answer. Cite using [doc_id] notation."
                },
                "citations_used": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of doc_ids you cited (must be from provided context only)"
                },
                "knowledge_gaps": {
                    "type": "string",
                    "description": "What the user asked that you couldn't answer from the provided documents (or empty string)"
                }
            },
            "required": ["answer", "citations_used", "knowledge_gaps"]
        }
    }

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "Answer using ONLY the provided documents. "
            "Cite with [doc_id]. If a document isn't in the context, you cannot cite it. "
            "Acknowledge gaps honestly."
        ),
        tools=[response_tool],
        tool_choice={"type": "tool", "name": "write_response"},
        messages=[{
            "role": "user",
            "content": f"## Retrieved Documents\n{context}\n\n## Question\n{query}"
        }]
    )

    for block in response.content:
        if block.type == "tool_use":
            data = block.input
            # Validate: only allow citations to retrieved doc IDs
            valid_ids = {doc["doc_id"] for doc in retrieved_docs}
            invalid = [cid for cid in data.get("citations_used", []) if cid not in valid_ids]

            # Build bibliography from cited docs
            bibliography = []
            for cid in data.get("citations_used", []):
                doc = next((d for d in retrieved_docs if d["doc_id"] == cid), None)
                if doc:
                    bibliography.append(
                        f"[{cid}] {doc['authors']} ({doc['year']}). {doc['title']}. {doc['url']}"
                    )

            return {
                "answer": data.get("answer", ""),
                "knowledge_gaps": data.get("knowledge_gaps", ""),
                "bibliography": bibliography,
                "invalid_citations": invalid,
                "citation_integrity": len(invalid) == 0,
            }

    return {"error": "No structured response"}

result = rag_answer("What are the key innovations in transformer and BERT architectures?")
print(result["answer"])
print("\n" + "\n".join(result["bibliography"]))
if result.get("knowledge_gaps"):
    print(f"\nGaps: {result['knowledge_gaps']}")
print(f"\nCitation integrity: {result['citation_integrity']}")
```

**Expected Token Savings:** RAG prevents hallucination entirely on covered topics — no correction cycles needed.

**Environment:** Python 3.9+, `anthropic>=0.40.0`. Replace simulated retrieval with real vector DB in production.

---

### Option 6 — Citation Confidence Scoring

Have the agent self-report confidence in each citation — flag low-confidence citations for human review.

```python
import anthropic
import json

client = anthropic.Anthropic()

CITATION_ASSESSMENT_TOOL = {
    "name": "provide_answer_with_citations",
    "description": "Provide answer with self-assessed citation confidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "The specific claim being cited"},
                        "source_description": {"type": "string", "description": "What you think the source is"},
                        "url": {"type": "string", "description": "URL if confident it exists, else empty string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "high=certain this source exists; medium=likely; low=uncertain"
                        },
                        "confidence_reason": {"type": "string", "description": "Why this confidence level"},
                    },
                    "required": ["claim", "source_description", "confidence", "confidence_reason"]
                }
            }
        },
        "required": ["answer", "citations"]
    }
}

CONFIDENCE_SYSTEM = """You are a research assistant. For every factual claim, assess your citation confidence:
- HIGH: You are certain this specific paper/article exists with this title/author/URL
- MEDIUM: You believe this exists but are not certain of exact details
- LOW: You know this topic is real but cannot confidently cite a specific source

Be honest — it's better to say LOW confidence than to fabricate a convincing URL."""

def answer_with_confidence(query: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CONFIDENCE_SYSTEM,
        tools=[CITATION_ASSESSMENT_TOOL],
        tool_choice={"type": "tool", "name": "provide_answer_with_citations"},
        messages=[{"role": "user", "content": query}]
    )

    for block in response.content:
        if block.type == "tool_use":
            data = block.input
            citations = data.get("citations", [])

            # Separate by confidence
            high = [c for c in citations if c["confidence"] == "high"]
            medium = [c for c in citations if c["confidence"] == "medium"]
            low = [c for c in citations if c["confidence"] == "low"]

            # Build answer with confidence markers
            answer = data["answer"]
            warnings = []

            if medium:
                warnings.append(
                    f"⚠️ {len(medium)} citation(s) have MEDIUM confidence — verify before using."
                )
            if low:
                warnings.append(
                    f"🚫 {len(low)} citation(s) have LOW confidence — do not cite without verification."
                )

            return {
                "answer": answer,
                "warnings": warnings,
                "citations": {
                    "high_confidence": high,
                    "medium_confidence": medium,
                    "low_confidence": low,
                },
                "safe_to_use": len(medium) == 0 and len(low) == 0,
            }

    return {"error": "No structured response"}

result = answer_with_confidence(
    "Explain scaling laws in large language models and cite relevant research."
)
print(result["answer"])
print("\nWarnings:", result.get("warnings", []))
print("\nHigh confidence citations:")
for c in result["citations"]["high_confidence"]:
    print(f"  ✓ {c['source_description']}: {c.get('url', 'no URL')}")
print("\nLow confidence citations:")
for c in result["citations"]["low_confidence"]:
    print(f"  ✗ {c['claim']} — {c['confidence_reason']}")
```

**Expected Token Savings:** Confidence scoring is free (no extra LLM call); enables users to verify medium/low citations themselves, avoiding full correction loops.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

## Comparison

| Option | Prevention | Detects Fabrication | Adds Latency | Requires Retrieval |
|--------|-----------|---------------------|-------------|-------------------|
| 1 — Grounded Documents | Pre-generation | Structural | No | Yes |
| 2 — Citation Tool + URL Validation | At generation | Yes | +1-5s/citation | No |
| 3 — Post-Generation Audit | Post-generation | Yes | +1-5s/URL | No |
| 4 — No-Citation Mode | Pre-generation | Structural | No | No |
| 5 — Full RAG Pipeline | Structural | Structural | No | Yes |
| 6 — Confidence Scoring | At generation | Partial | No | No |

**Start with Option 4** (honest disclosure) for any agent without a retrieval system — zero cost, immediate improvement. Add **Option 1** or **Option 5** (RAG) when you have a document corpus. Use **Option 2** (citation tool + URL validation) when users will publish the citations externally.
