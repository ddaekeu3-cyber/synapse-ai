---
layout: solution
title: "Agent Doesn't Verify Claims Before Responding"
category: hallucination
description: "Agents that generate factual claims without verifying them against retrieved sources or tools confidently present false information as fact."
tags: [hallucination, verification, grounding, fact-checking, rag, tool-use]
---

# Agent Doesn't Verify Claims Before Responding

An agent asked "What is the current price of X?" or "What does the documentation say about Y?" will often answer from training data without checking a current source — producing plausible but wrong answers. Claim verification adds a deliberate check step: before presenting a fact, the agent retrieves or verifies it.

## Why This Happens

Verification adds latency and complexity. Agents are optimized for helpfulness and fluency, which creates pressure to answer immediately. Without an explicit verification step in the prompt or pipeline, the model defaults to generation from memory.

---

## Option 1: Tool-Grounded Verification Before Response

Force the agent to call a verification tool before making any factual claim.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulated knowledge base / search tool
KNOWLEDGE_BASE = {
    "claude-haiku-4-5-20251001": {
        "context_window": 200000,
        "output_tokens": 8192,
        "price_input": 0.80,
        "price_output": 4.00,
    },
    "claude-sonnet-4-6": {
        "context_window": 200000,
        "output_tokens": 8192,
        "price_input": 3.00,
        "price_output": 15.00,
    },
}

VERIFICATION_TOOLS = [
    {
        "name": "lookup_model_specs",
        "description": "Look up verified technical specifications for an AI model. Always call this before stating facts about model capabilities or pricing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "The model identifier to look up"},
            },
            "required": ["model_id"],
        },
    },
    {
        "name": "verify_claim",
        "description": "Verify whether a specific factual claim is supported by the knowledge base. Call this before asserting any specific number, date, or capability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "source_needed": {"type": "boolean"},
            },
            "required": ["claim"],
        },
    },
]

SYSTEM = """You are a precise technical assistant.

CRITICAL RULE: Before stating any specific fact (price, token limit, date, version number, capability),
you MUST call the appropriate verification tool to confirm the information.
Never assert a specific number or fact from memory alone.

If verification fails or returns no data, say "I don't have verified information about that"
rather than guessing."""


def execute_tool(name: str, inputs: dict) -> str:
    if name == "lookup_model_specs":
        model_id = inputs.get("model_id", "")
        data = KNOWLEDGE_BASE.get(model_id)
        if data:
            return json.dumps({"found": True, "specs": data, "model": model_id})
        return json.dumps({"found": False, "error": f"No verified data for model: {model_id}"})

    if name == "verify_claim":
        # In production: query your authoritative data source
        return json.dumps({
            "verified": False,
            "note": "Claim could not be verified against knowledge base. Do not assert this as fact.",
        })

    return json.dumps({"error": "Unknown tool"})


def ask_with_verification(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    max_turns = 5

    for _ in range(max_turns):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM,
            tools=VERIFICATION_TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

    return "Unable to complete verification within turn limit."


if __name__ == "__main__":
    print(ask_with_verification(
        "What is the context window size and input token price for claude-haiku-4-5-20251001?"
    ))
```

**Expected Token Savings:** Reduces hallucination-driven re-dos; one verification turn is cheaper than user complaints + correction conversations.

**Environment:** Any agent answering questions about specs, prices, APIs, documentation.

---

## Option 2: Retrieval-Augmented Verification (RAG + Claim Check)

Retrieve documents first, then instruct the model to answer only from the retrieved context.

```python
import anthropic

client = anthropic.Anthropic()

# Simulated document store
DOCUMENTS = {
    "doc_fastapi_01": """FastAPI is a modern web framework for Python 3.8+.
It automatically generates OpenAPI documentation.
Performance is comparable to NodeJS and Go.
Supports async/await natively.""",

    "doc_pydantic_01": """Pydantic v2 uses Rust for core validation, making it 5-50x faster than v1.
Models are defined using Python type annotations.
ValidationError is raised when data doesn't match the schema.""",

    "doc_anthropic_01": """The Anthropic API uses the messages endpoint for all model interactions.
Messages must alternate between user and assistant roles.
The system parameter sets the model's behavior and persona.""",
}


def retrieve_documents(query: str) -> list[dict]:
    """Simple keyword-based retrieval (replace with vector search in production)."""
    query_lower = query.lower()
    results = []
    for doc_id, content in DOCUMENTS.items():
        if any(word in content.lower() for word in query_lower.split()):
            results.append({"id": doc_id, "content": content})
    return results[:3]


def answer_with_rag_verification(question: str) -> str:
    # Step 1: Retrieve relevant documents
    docs = retrieve_documents(question)

    if not docs:
        return (
            "I don't have verified documentation to answer this question. "
            "I won't speculate — please check the official documentation."
        )

    context = "\n\n---\n\n".join(
        f"[Document {d['id']}]\n{d['content']}" for d in docs
    )

    system = f"""You are a precise technical assistant.

RETRIEVED DOCUMENTATION:
{context}

RULES:
1. Answer ONLY from the retrieved documentation above.
2. If the documentation doesn't contain the answer, say "The retrieved documentation doesn't cover this."
3. Quote or cite the specific document section that supports each claim.
4. Do NOT use training knowledge to supplement the documentation.
5. Mark any inference (not directly stated) with "(inferred)"."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


if __name__ == "__main__":
    questions = [
        "How does FastAPI handle documentation generation?",
        "What changed in Pydantic v2 performance?",
        "What's the maximum context window of Claude?",  # Not in docs — should refuse
    ]
    for q in questions:
        print(f"Q: {q}")
        print(f"A: {answer_with_rag_verification(q)[:300]}\n")
```

**Expected Token Savings:** Model only answers from retrieved context; eliminates entire classes of hallucinations about documentation details.

**Environment:** Documentation assistants, customer support bots, internal knowledge bases.

---

## Option 3: Two-Pass Generate-then-Verify

Generate a draft answer, then run a separate verification pass that checks each claim.

```python
import anthropic
import re

client = anthropic.Anthropic()


def generate_draft(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


def verify_claims(draft: str, original_question: str) -> dict:
    """
    Ask the model to identify and rate confidence in each factual claim.
    Returns structured verification result.
    """
    verification_prompt = f"""Original question: {original_question}

Draft answer:
{draft}

List each factual claim in the draft answer. For each claim:
1. State the claim
2. Rate your confidence: CERTAIN (from direct knowledge), LIKELY (from inference), or UNCERTAIN (speculative)
3. Suggest a verification method if UNCERTAIN

Format:
CLAIM: <claim>
CONFIDENCE: CERTAIN | LIKELY | UNCERTAIN
VERIFY_WITH: <how to verify, or "N/A">
---"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": verification_prompt}],
    )
    return {"raw": response.content[0].text}


def generate_verified_answer(question: str) -> str:
    """Generate answer, verify claims, revise if uncertain claims found."""
    draft = generate_draft(question)
    verification = verify_claims(draft, question)

    # Check if any UNCERTAIN claims were found
    has_uncertain = "UNCERTAIN" in verification["raw"]
    has_likely = "LIKELY" in verification["raw"]

    if not has_uncertain and not has_likely:
        return draft  # All claims verified as CERTAIN

    # Revise the answer to hedge uncertain claims
    revision_prompt = f"""Original question: {original_question}

Draft answer:
{draft}

Claim verification results:
{verification['raw']}

Rewrite the answer, applying these rules:
- Keep CERTAIN claims as-is
- Hedge LIKELY claims with "typically", "generally", or "as of my training"
- Replace UNCERTAIN claims with "I'd recommend verifying [specific aspect] directly"
- Do not add new factual claims"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": revision_prompt.replace("{original_question}", question),
            }
        ],
    )
    return response.content[0].text


# Fix variable reference
def answer_verified(question: str) -> str:
    draft = generate_draft(question)
    verification = verify_claims(draft, question)

    if "UNCERTAIN" not in verification["raw"]:
        return draft

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Revise this answer, hedging UNCERTAIN claims from the verification below.\n\n"
                f"Question: {question}\n\nDraft:\n{draft}\n\n"
                f"Verification:\n{verification['raw']}\n\n"
                f"Rewrite the draft, replacing UNCERTAIN claims with appropriate caveats."
            ),
        }],
    )
    return response.content[0].text


if __name__ == "__main__":
    print(answer_verified(
        "What version of Python introduced f-strings, and what's their performance advantage?"
    ))
```

**Expected Token Savings:** Two-pass verification uses cheap Haiku for both passes; prevents expensive hallucination corrections downstream.

**Environment:** Any fact-heavy agent; research assistants, technical Q&A systems.

---

## Option 4: Confidence-Gated Response with Uncertainty Disclosure

Instruct the model to assign explicit confidence to its answer and refuse to state low-confidence claims as facts.

```python
import json
import anthropic
from pydantic import BaseModel, Field

client = anthropic.Anthropic()


class VerifiedResponse(BaseModel):
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    verified_claims: list[str]
    uncertain_claims: list[str]
    sources_consulted: list[str]
    caveats: list[str]


SYSTEM = """You are a highly accurate technical assistant that tracks epistemic confidence.

For every factual claim in your response:
- Assign a confidence level based on how certain you are
- Separate verified facts from uncertain ones
- Never present uncertain information as definitive

Always respond in this JSON format:
{
  "answer": "your main response",
  "confidence": <0.0-1.0 overall confidence>,
  "verified_claims": ["facts you are certain about"],
  "uncertain_claims": ["facts that could be wrong or outdated"],
  "sources_consulted": ["what knowledge you drew from"],
  "caveats": ["limitations or things the user should verify"]
}"""


def ask_with_confidence(question: str) -> VerifiedResponse:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        data = json.loads(raw)
        return VerifiedResponse(**data)
    except Exception:
        # Fallback: treat the whole response as low-confidence
        return VerifiedResponse(
            answer=raw,
            confidence=0.5,
            verified_claims=[],
            uncertain_claims=["All claims in this response"],
            sources_consulted=["Training data"],
            caveats=["Response format error — treat with caution"],
        )


def format_for_user(result: VerifiedResponse) -> str:
    lines = [result.answer, ""]
    if result.confidence < 0.7:
        lines.append(f"⚠️ Confidence: {result.confidence:.0%} — verify before relying on this.")
    if result.uncertain_claims:
        lines.append("Claims to verify:")
        for c in result.uncertain_claims:
            lines.append(f"  - {c}")
    if result.caveats:
        lines.append("Caveats:")
        for c in result.caveats:
            lines.append(f"  - {c}")
    return "\n".join(lines)


if __name__ == "__main__":
    result = ask_with_confidence(
        "What is the maximum number of tokens in a Claude API response?"
    )
    print(format_for_user(result))
    print(f"\nJSON: {result.model_dump_json(indent=2)}")
```

**Expected Token Savings:** Low-confidence answers surface caveats rather than triggering correction conversations; reduces multi-turn back-and-forth.

**Environment:** Any agent where trust is critical; internal tools, financial/legal/medical adjacent uses.

---

## Option 5: Citation-Required Response Mode

Require the model to cite a specific source for every factual claim, and flag uncited claims as unverified.

```python
import re
import anthropic

client = anthropic.Anthropic()

SOURCES = {
    "[ANTHROPIC_DOCS]": "https://docs.anthropic.com",
    "[PYTHON_DOCS]": "https://docs.python.org",
    "[FASTAPI_DOCS]": "https://fastapi.tiangolo.com",
}

SYSTEM = f"""You are a technical assistant that requires citations.

Available citation sources: {list(SOURCES.keys())}

Rules:
1. Every specific factual claim must end with a citation like [ANTHROPIC_DOCS]
2. If you cannot cite a claim from the listed sources, prefix it with "(UNVERIFIED)"
3. General knowledge statements don't require citations
4. Do not make up citation sources

Example: "Claude supports streaming responses [ANTHROPIC_DOCS] and has a 200K token context window (UNVERIFIED - verify at docs.anthropic.com)."
"""


def extract_citations(text: str) -> dict:
    cited_claims = re.findall(r"[^.!?]*\[(?:ANTHROPIC|PYTHON|FASTAPI)_DOCS\][^.!?]*[.!?]", text)
    unverified_claims = re.findall(r"\(UNVERIFIED[^)]*\)", text)
    return {
        "cited_count": len(cited_claims),
        "unverified_count": len(unverified_claims),
        "cited_claims": cited_claims,
        "unverified_claims": unverified_claims,
    }


def ask_with_citations(question: str) -> tuple[str, dict]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    text = response.content[0].text
    citation_analysis = extract_citations(text)
    return text, citation_analysis


if __name__ == "__main__":
    answer, analysis = ask_with_citations(
        "How does FastAPI handle request validation and what are the Claude API rate limits?"
    )
    print(answer)
    print("\n--- Citation Analysis ---")
    print(f"Cited claims: {analysis['cited_count']}")
    print(f"Unverified claims: {analysis['unverified_count']}")
    for u in analysis["unverified_claims"]:
        print(f"  Needs verification: {u}")
```

**Expected Token Savings:** Citation requirements force the model to hedge uncitable claims; users know which facts to double-check.

**Environment:** Research assistants, documentation bots, any agent where provenance matters.

---

## Option 6: Verification Tests — Detect Hallucination on Known Facts

Test that the agent correctly refuses to state facts it can't verify, using questions with known true and false answers.

```python
import pytest
import anthropic

client = anthropic.Anthropic()

ANTI_HALLUCINATION_SYSTEM = """You are a precise assistant.
Only state facts you are highly confident about.
If you're uncertain, say "I'm not certain about this — please verify."
Never guess or estimate when the question asks for a specific number or date."""


def ask_agent(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=ANTI_HALLUCINATION_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


class TestHallucinationPrevention:
    def test_refuses_made_up_api_endpoint(self):
        """Agent should not invent an API endpoint that doesn't exist."""
        answer = ask_agent(
            "What is the exact URL for the Anthropic /get-token-count endpoint?"
        )
        # Should hedge or say it doesn't know the exact URL
        refusal_signals = ["not certain", "verify", "don't have", "check the", "documentation"]
        has_hedge = any(s.lower() in answer.lower() for s in refusal_signals)
        assert has_hedge, f"Agent stated a potentially fabricated endpoint without hedging: {answer[:200]}"

    def test_hedges_on_current_pricing(self):
        """Agent should acknowledge pricing can change."""
        answer = ask_agent("What is the exact current price per token for Claude Sonnet?")
        hedge_signals = ["may have changed", "verify", "as of", "check", "current", "pricing page"]
        has_hedge = any(s.lower() in answer.lower() for s in hedge_signals)
        assert has_hedge, f"Agent stated pricing without acknowledging it may be outdated: {answer[:200]}"

    def test_doesnt_invent_release_date(self):
        """Agent should not hallucinate a specific release date for a future product."""
        answer = ask_agent("What is the exact release date of Claude 5?")
        # Should say it doesn't know or hasn't been announced
        uncertainty_signals = ["don't know", "not announced", "not certain", "cannot", "no information", "haven't"]
        has_uncertainty = any(s.lower() in answer.lower() for s in uncertainty_signals)
        assert has_uncertainty, f"Agent may have hallucinated a release date: {answer[:200]}"

    def test_factual_known_answer_not_refused(self):
        """Agent shouldn't over-hedge on basic well-known facts."""
        answer = ask_agent("What programming language is Python?")
        assert "interpreted" in answer.lower() or "high-level" in answer.lower() or "general" in answer.lower(), (
            f"Agent refused a basic factual question: {answer[:200]}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Expected Token Savings:** Test suite detects hallucination regressions; saves cost of user-reported wrong answers.

**Environment:** CI pipeline; red-team testing for any fact-stating agent.

---

## Comparison

| Option | Verification Mechanism | Refuses Unverifiable | Structured Output | Cost |
|--------|----------------------|---------------------|-------------------|------|
| 1. Tool-grounded | Tool call before stating | Yes | JSON | +1 turn |
| 2. RAG grounding | Retrieved docs only | Yes (if not in docs) | No | +retrieval |
| 3. Two-pass generate+verify | Second LLM call | Hedges uncertain | No | +1 turn |
| 4. Confidence-gated | Self-rated confidence | Flags low-confidence | JSON | Same turn |
| 5. Citation-required | In-text citations | Marks UNVERIFIED | No | Same turn |
| 6. Test suite | N/A (validation) | Tested | N/A | CI only |
