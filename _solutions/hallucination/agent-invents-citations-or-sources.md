---
layout: solution
title: "Agent Invents Citations, URLs, or Sources That Don't Exist"
category: hallucination
description: "Agent provides references to support its answer, but the papers, URLs, or sources are fabricated. The DOI resolves to nothing. The Wikipedia page doesn't exist. The study was never published. User trusts the hallucinated source."
tags: [hallucination, citations, sources, references, urls, fabricated, grounding, rag]
---

# Agent Invents Citations, URLs, or Sources That Don't Exist

## Symptom
- Agent cites "Smith et al. (2021) in Nature" — paper doesn't exist
- Provided URL returns 404 — page was never real
- Wikipedia article title cited doesn't exist
- Agent fabricates a specific statistic with a source: "According to WHO (2023), 73% of..."
- Research summary includes 5 references, 3 of which are completely made up

## Root Cause
LLMs are trained to produce fluent, authoritative-sounding text. When asked for sources, they generate plausible-looking citations rather than retrieving real ones — because they have no real-time search capability. The model knows what a citation looks like and produces one that "sounds right" based on training data patterns. This is reinforced by the fact that users rarely check citations, so the model was never penalized for fabricating them.

## Fix

### Option 1: Never ask the model to generate citations — provide sources first

```python
async def answer_with_verified_sources(
    query: str,
    sources: list[dict],  # Pre-fetched, verified sources
    client
) -> str:
    """
    Ground the agent in real sources before answering.
    The model synthesizes from provided text — cannot invent beyond what's given.
    """
    source_context = "\n\n".join([
        f"Source {i+1}: [{s['title']}]({s['url']})\n{s['excerpt']}"
        for i, s in enumerate(sources)
    ])

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        system=(
            "Answer using ONLY the sources provided below. "
            "If the sources don't contain the answer, say 'The provided sources don't address this.' "
            "When citing, use the exact title and URL from the sources. "
            "Do NOT add citations beyond what is provided."
        ),
        messages=[{
            "role": "user",
            "content": f"Sources:\n{source_context}\n\nQuestion: {query}"
        }],
        max_tokens=1024
    )
    return response.content[0].text

# Real workflow:
search_results = await search_web(query)  # Actual search results
answer = await answer_with_verified_sources(query, search_results, client)
# → Agent can only cite what you gave it — fabrication impossible
```

### Option 2: Verify citations before returning them

```python
import httpx
import asyncio

async def verify_url(url: str, timeout: float = 10.0) -> dict:
    """Check if a URL is actually reachable"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.head(url, timeout=timeout, follow_redirects=True)
            return {"url": url, "valid": resp.status_code < 400, "status": resp.status_code}
    except Exception as e:
        return {"url": url, "valid": False, "error": str(e)}

async def extract_and_verify_citations(agent_response: str) -> dict:
    """
    Extract URLs and DOIs from agent response, verify each one exists.
    Returns verified and failed citations.
    """
    import re

    # Extract URLs
    urls = re.findall(r'https?://[^\s\)\]"]+', agent_response)

    # Extract DOIs
    dois = re.findall(r'10\.\d{4,}/\S+', agent_response)
    doi_urls = [f"https://doi.org/{doi}" for doi in dois]

    all_urls = list(set(urls + doi_urls))
    if not all_urls:
        return {"verified": [], "failed": [], "no_citations": True}

    # Check all URLs in parallel
    tasks = [verify_url(url) for url in all_urls]
    results = await asyncio.gather(*tasks)

    verified = [r for r in results if r["valid"]]
    failed = [r for r in results if not r["valid"]]

    if failed:
        print(f"WARNING: {len(failed)} citations failed verification:")
        for f in failed:
            print(f"  {f['url']}: {f.get('error', f'HTTP {f.get(\"status\")}')}")

    return {"verified": verified, "failed": failed}

# After getting agent response:
response = await agent.answer(query)
citation_check = await extract_and_verify_citations(response)
if citation_check.get("failed"):
    response += f"\n\n⚠️ Note: {len(citation_check['failed'])} citation(s) could not be verified."
```

### Option 3: System prompt that explicitly prohibits fabricated sources

```python
NO_FABRICATION_SYSTEM = """
Citation rules (strictly enforced):

1. NEVER cite a paper, article, URL, or study unless you can verify it exists.
   If you are uncertain whether a source exists, DO NOT cite it.

2. If you need to reference information without a verified source, say:
   "Based on my training data (source not verified):" before the claim.

3. Do NOT produce:
   - Specific DOIs unless you are certain they resolve
   - Author names + year + journal unless you are certain the paper exists
   - URLs unless you have confirmed the page exists
   - Statistics attributed to specific organizations unless from a provided source

4. If asked to cite sources and you have none: respond with
   "I don't have verified sources for this. Please use a search tool to find citations."

5. You MAY say "research has shown" or "studies suggest" without specific citations
   when making general knowledge claims.
"""

# This doesn't eliminate the risk entirely, but dramatically reduces fabrication rate
```

### Option 4: RAG pipeline — retrieve real documents, cite only those

```python
from dataclasses import dataclass

@dataclass
class Document:
    title: str
    url: str
    content: str
    source_type: str  # "web", "pdf", "database"
    retrieved_at: str

class VerifiedRAGAgent:
    """
    Agent that answers ONLY from retrieved documents.
    Every citation is a document that was actually fetched.
    """

    def __init__(self, retriever, llm_client):
        self.retriever = retriever
        self.client = llm_client
        self.retrieved_docs: list[Document] = []

    async def answer(self, query: str, n_docs: int = 5) -> dict:
        # Step 1: Retrieve real documents
        self.retrieved_docs = await self.retriever.search(query, limit=n_docs)

        if not self.retrieved_docs:
            return {
                "answer": "I couldn't find any relevant sources for this query.",
                "citations": []
            }

        # Step 2: Build context from real documents
        context = "\n\n---\n\n".join([
            f"[DOC{i+1}] {doc.title} ({doc.url})\n{doc.content[:1000]}"
            for i, doc in enumerate(self.retrieved_docs)
        ])

        # Step 3: Answer using only provided context
        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            system=(
                "Answer using ONLY the documents provided. "
                "Cite documents as [DOC1], [DOC2], etc. "
                "If information is not in the documents, say so."
            ),
            messages=[{"role": "user", "content": f"Documents:\n{context}\n\nQuestion: {query}"}],
            max_tokens=1024
        )

        # Step 4: Map [DOC1] references to real document metadata
        answer_text = response.content[0].text
        cited_docs = []
        import re
        for match in re.finditer(r'\[DOC(\d+)\]', answer_text):
            doc_idx = int(match.group(1)) - 1
            if 0 <= doc_idx < len(self.retrieved_docs):
                doc = self.retrieved_docs[doc_idx]
                if doc not in cited_docs:
                    cited_docs.append(doc)

        return {
            "answer": answer_text,
            "citations": [{"title": d.title, "url": d.url} for d in cited_docs]
        }
```

### Option 5: Post-process to flag unverified claims

```python
import re

CLAIM_PATTERNS = [
    r'according to [\w\s]+\(\d{4}\)',           # "According to Smith (2021)"
    r'[\w\s]+ et al\.\s*\(\d{4}\)',             # "Smith et al. (2021)"
    r'in [\w\s]+,\s+\w+ et al',                 # "In Nature, Smith et al"
    r'\d+% of [\w\s]+ according to',            # "73% of X according to"
    r'doi\.org/10\.\d+/\S+',                    # DOI links
    r'https?://[^\s]+',                          # Any URL
]

def flag_unverified_claims(text: str, verified_urls: set = None) -> str:
    """
    Add [UNVERIFIED] tag to claims that reference sources not in the verified set.
    """
    verified_urls = verified_urls or set()
    flagged = text

    for pattern in CLAIM_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            matched_text = match.group(0)
            # Check if it's a URL that we've verified
            if matched_text.startswith("http"):
                if matched_text not in verified_urls:
                    flagged = flagged.replace(
                        matched_text,
                        f"{matched_text} [UNVERIFIED]"
                    )
            else:
                # Citation-style reference — always flag as unverified
                flagged = flagged.replace(
                    matched_text,
                    f"{matched_text} [UNVERIFIED — please check this source]"
                )

    return flagged

# Usage:
raw_response = agent.answer("What does research say about X?")
safe_response = flag_unverified_claims(raw_response)
```

### Option 6: Structured output that separates claims from citations

```python
from pydantic import BaseModel, Field

class VerifiedClaim(BaseModel):
    claim: str
    source_provided: bool  # Was a real source given to the agent?
    source_title: str = ""
    source_url: str = ""
    confidence: str  # "high" (from source), "medium" (training data), "low" (uncertain)

class GroundedResponse(BaseModel):
    summary: str
    claims: list[VerifiedClaim]
    disclaimer: str = ""

async def get_grounded_response(query: str, sources: list[dict], client) -> GroundedResponse:
    """
    Structure output to explicitly mark which claims have real sources.
    """
    import json

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        system=(
            "Return a JSON object with: summary (string), claims (list of objects with: "
            "claim, source_provided (bool), source_title, source_url, confidence). "
            "source_provided must be TRUE only for claims directly supported by the provided sources. "
            "For knowledge from training data, set source_provided=false and confidence='medium'. "
            "No other text."
        ),
        messages=[{
            "role": "user",
            "content": f"Sources: {json.dumps(sources)}\n\nAnswer: {query}"
        }],
        max_tokens=1024
    )

    data = json.loads(response.content[0].text)
    return GroundedResponse(**data)

# Consumer knows which claims have real sources:
result = await get_grounded_response(query, real_sources, client)
verified = [c for c in result.claims if c.source_provided]
unverified = [c for c in result.claims if not c.source_provided]
```

## Citation Fabrication Risk by Request Type

| Request type | Fabrication risk | Strategy |
|---|---|---|
| "Cite sources for X" (no context) | Critical | Refuse; use search tool first |
| "Summarize this article" (article given) | Low | Agent cites provided text |
| "What does research say about X?" | High | RAG pipeline required |
| "Give me a statistic about X" | High | Verify any number with source |
| "What is X?" (factual) | Medium | Flag specific attributions |
| "Based on these papers [attached]" | Low | Ground in provided content |

## Expected Token Savings
User discovers fabricated source, escalates, agent re-researches: ~20,000 tokens
RAG pipeline prevents fabrication entirely: 0 wasted

## Environment
- Any agent that answers research questions, generates reports, or produces cited content
- Source: direct experience; citation hallucination is the most trust-damaging agent failure mode
