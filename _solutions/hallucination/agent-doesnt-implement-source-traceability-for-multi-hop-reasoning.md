---
layout: solution
title: "Agent Doesn't Implement Source Traceability for Multi-Hop Reasoning"
category: hallucination
description: "In multi-hop reasoning chains, errors introduced at early steps propagate silently into conclusions. These patterns show how to tag each reasoning step with its source so hallucinations are caught before they compound."
tags: [hallucination, traceability, reasoning, multi-hop, citation, anthropic]
---

## Problem

A multi-hop reasoning chain — "Find the CEO, then find their prior company, then find that company's revenue" — fails silently when step 2 hallucinates a name that step 3 treats as fact. Without source tagging at each hop, the final answer looks confident but was built on a fabricated intermediate. Source traceability forces each step to declare what evidence it used, making the chain auditable and break-points detectable.

---

### Option 1: Explicit Provenance Tags on Each Reasoning Step

Require the model to tag every claim with its source as it reasons.

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

TRACED_REASONING_PROMPT = """You are a careful analyst. For each step of your reasoning, you MUST tag every claim with its source.

Use format: claim [SOURCE: document_id | inferred | prior_step_N | unknown]

Available sources:
{sources}

Question: {question}

Reason step-by-step. Tag EVERY factual claim with [SOURCE: ...]. At the end, provide a final answer tagged [SOURCE: prior_step_N] for each component.

Begin:"""

def extract_unsourced_claims(reasoning: str) -> list[str]:
    """Find claims that are missing SOURCE tags."""
    sentences = re.split(r'(?<=[.!?])\s+', reasoning)
    unsourced = []
    for sent in sentences:
        if len(sent.split()) > 5:  # skip short sentences
            if "[SOURCE:" not in sent and "ANSWER:" not in sent.lower():
                unsourced.append(sent.strip())
    return unsourced

def parse_sources_used(reasoning: str) -> dict:
    """Extract which sources were cited."""
    pattern = r'\[SOURCE:\s*([^\]]+)\]'
    sources = re.findall(pattern, reasoning)
    source_counts = {}
    for s in sources:
        s = s.strip()
        source_counts[s] = source_counts.get(s, 0) + 1
    return source_counts

def traced_reasoning(question: str, context_docs: dict[str, str]) -> dict:
    sources_text = "\n".join(f"- {k}: {v[:300]}" for k, v in context_docs.items())

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": TRACED_REASONING_PROMPT.format(
                sources=sources_text,
                question=question,
            ),
        }],
    )
    reasoning = response.content[0].text

    unsourced = extract_unsourced_claims(reasoning)
    sources_used = parse_sources_used(reasoning)

    result = {
        "reasoning": reasoning,
        "sources_cited": sources_used,
        "unsourced_claims": unsourced,
        "traceability_score": 1.0 - len(unsourced) / max(len(re.split(r'[.!?]', reasoning)), 1),
    }

    if unsourced:
        print(f"[WARNING] {len(unsourced)} unsourced claims detected:")
        for claim in unsourced[:3]:
            print(f"  - {claim[:80]}")

    return result

if __name__ == "__main__":
    question = "Based on the documents, what was the company's revenue trend and its likely cause?"

    docs = {
        "financial_report_2023": "Total revenue for FY2023 was $4.2B, up 18% from $3.6B in 2022. Growth was driven primarily by the cloud services division which grew 42% YoY.",
        "press_release": "CEO Jane Smith announced that the company's new enterprise contracts contributed significantly to Q4 performance. Five Fortune 500 companies signed multi-year agreements.",
        "analyst_note": "Market share in cloud services increased from 8% to 11% during 2023. Primary competitor lost two major contracts due to outage incidents.",
    }

    result = traced_reasoning(question, docs)
    print(f"\nTraceability score: {result['traceability_score']:.0%}")
    print(f"Sources cited: {result['sources_cited']}")
    print(f"\nReasoning:\n{result['reasoning'][:600]}")

# Expected Token Savings: Traceability is a prompting technique; no extra API calls; catches compounding errors early
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Step-by-Step Chain with Inter-Step Verification

Execute each reasoning step separately and verify that each step's output is grounded in available evidence before passing it to the next step.

```python
import json
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

STEP_EXECUTION_PROMPT = """Execute this reasoning step using ONLY the provided context.

Step: {step}
Context: {context}
Previous findings: {prior_findings}

Answer the step. State what evidence you used. If the context is insufficient, say "INSUFFICIENT EVIDENCE" rather than guessing.

Response format:
FINDING: <your finding>
EVIDENCE: <exact quote or reference from context>
CONFIDENCE: high|medium|low|insufficient"""

VERIFICATION_PROMPT = """Verify that this finding is supported by the context.

Finding: {finding}
Evidence cited: {evidence}
Available context: {context}

Is the finding directly supported? Answer: SUPPORTED | PARTIALLY_SUPPORTED | UNSUPPORTED
Reason: <one sentence>"""

async def execute_step(
    step: str,
    context: str,
    prior_findings: list[dict],
) -> dict:
    prior_text = "\n".join(f"Step {i+1}: {f['finding']}" for i, f in enumerate(prior_findings))

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": STEP_EXECUTION_PROMPT.format(
                step=step,
                context=context[:1500],
                prior_findings=prior_text or "None",
            ),
        }],
    )
    raw = response.content[0].text
    result = {"step": step, "raw": raw, "finding": "", "evidence": "", "confidence": "low"}

    for line in raw.split("\n"):
        if line.startswith("FINDING:"):
            result["finding"] = line[8:].strip()
        elif line.startswith("EVIDENCE:"):
            result["evidence"] = line[9:].strip()
        elif line.startswith("CONFIDENCE:"):
            result["confidence"] = line[11:].strip().lower()

    return result

async def verify_step(step_result: dict, context: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": VERIFICATION_PROMPT.format(
                finding=step_result["finding"],
                evidence=step_result["evidence"],
                context=context[:1000],
            ),
        }],
    )
    raw = response.content[0].text.strip()
    lines = raw.split("\n")
    verdict = lines[0].strip() if lines else "UNSUPPORTED"
    reason = lines[1].replace("Reason:", "").strip() if len(lines) > 1 else ""
    return {**step_result, "verification": verdict, "verification_reason": reason}

async def traced_chain_of_thought(
    reasoning_steps: list[str],
    context: str,
    halt_on_unsupported: bool = True,
) -> list[dict]:
    chain = []
    prior_findings = []

    for i, step in enumerate(reasoning_steps):
        print(f"[step {i+1}] {step[:60]}")
        step_result = await execute_step(step, context, prior_findings)

        if "insufficient" in step_result["confidence"]:
            print(f"  ⚠ INSUFFICIENT EVIDENCE — cannot proceed")
            step_result["verification"] = "HALTED"
            chain.append(step_result)
            break

        verified = await verify_step(step_result, context)
        chain.append(verified)

        verdict = verified["verification"]
        icon = "✓" if "SUPPORTED" == verdict else "~" if "PARTIALLY" in verdict else "✗"
        print(f"  {icon} {verdict}: {verified['finding'][:60]}")

        if verdict == "UNSUPPORTED" and halt_on_unsupported:
            print(f"  HALT: unsupported finding, stopping chain")
            break

        prior_findings.append(verified)

    return chain

if __name__ == "__main__":
    async def main():
        context = """
        Acme Corp Q4 2023 Report: Revenue was $1.2B, up 22% YoY.
        CEO John Davis has led the company since 2019.
        Previously, Davis was CTO at TechStart Inc from 2015-2019.
        TechStart was acquired by MegaCorp in 2020 for $800M.
        """

        steps = [
            "Who is the current CEO of Acme Corp?",
            "Where did the CEO work before Acme Corp?",
            "What happened to TechStart Inc?",
            "What was TechStart's acquisition price?",
        ]

        chain = await traced_chain_of_thought(steps, context)
        print(f"\n=== Chain complete: {len(chain)} steps ===")
        for i, step in enumerate(chain):
            print(f"  Step {i+1}: {step['finding'][:60]} [{step.get('verification', 'N/A')}]")
    asyncio.run(main())

# Expected Token Savings: Haiku for verification (cheap); stops chain early on hallucination, saving remaining step costs
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Knowledge Graph Tracing — Map Claims to Nodes

Build a knowledge graph of facts during reasoning and trace each conclusion back to source nodes.

```python
import json
import re
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class KnowledgeNode:
    id: str
    claim: str
    source: str        # "document:doc_id" | "inferred:step_N" | "unknown"
    confidence: float  # 0.0-1.0
    derived_from: list[str] = field(default_factory=list)  # parent node IDs

@dataclass
class KnowledgeGraph:
    nodes: dict[str, KnowledgeNode] = field(default_factory=dict)

    def add(self, node: KnowledgeNode) -> None:
        self.nodes[node.id] = node

    def get_provenance_chain(self, node_id: str) -> list[str]:
        """Trace all ancestor sources for a given node."""
        node = self.nodes.get(node_id)
        if not node:
            return []
        if not node.derived_from:
            return [node.source]
        parents = []
        for parent_id in node.derived_from:
            parents.extend(self.get_provenance_chain(parent_id))
        return list(set(parents))

    def has_unknown_provenance(self) -> list[str]:
        return [nid for nid, n in self.nodes.items() if n.source == "unknown"]

GRAPH_EXTRACTION_PROMPT = """Extract facts from these documents as a knowledge graph.

Documents:
{documents}

For each fact, output JSON lines:
{{"id": "f1", "claim": "...", "source": "document:<doc_id>", "confidence": 0.9, "derived_from": []}}

Output ONLY valid JSON lines, one per fact:"""

INFERENCE_PROMPT = """Using these known facts, answer the question by making inferences.

Known facts (as JSON):
{facts}

Question: {question}

For each inference step, output:
{{"id": "i1", "claim": "...", "source": "inferred:step_1", "confidence": 0.7, "derived_from": ["f1", "f2"]}}

Chain your reasoning. Output JSON lines only:"""

def parse_json_lines(text: str) -> list[dict]:
    results = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return results

def build_knowledge_graph(question: str, documents: dict[str, str]) -> KnowledgeGraph:
    graph = KnowledgeGraph()

    # Step 1: Extract base facts from documents
    doc_text = "\n\n".join(f"[{k}]: {v}" for k, v in documents.items())
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": GRAPH_EXTRACTION_PROMPT.format(documents=doc_text),
        }],
    )
    for item in parse_json_lines(response.content[0].text):
        graph.add(KnowledgeNode(**item))

    print(f"[graph] extracted {len(graph.nodes)} base facts")

    # Step 2: Infer answer
    facts_text = "\n".join(json.dumps({"id": n.id, "claim": n.claim}) for n in graph.nodes.values())
    inference_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": INFERENCE_PROMPT.format(facts=facts_text, question=question),
        }],
    )
    for item in parse_json_lines(inference_response.content[0].text):
        graph.add(KnowledgeNode(**item))

    print(f"[graph] total nodes: {len(graph.nodes)}")
    unknown = graph.has_unknown_provenance()
    if unknown:
        print(f"[WARNING] unknown provenance nodes: {unknown}")

    return graph

if __name__ == "__main__":
    question = "What is the connection between the two companies mentioned, and what does it imply for the market?"
    documents = {
        "news_a": "TechCorp announced a $500M acquisition of DataSoft on January 15, 2024. DataSoft has 200 enterprise clients and $80M ARR.",
        "news_b": "TechCorp's main competitor, CloudBase, lost market share last year. Industry analysts predict consolidation will continue.",
        "analyst": "Post-acquisition, TechCorp will control 35% of the SMB data management market. The acquisition price implies a 6.25x ARR multiple.",
    }

    graph = build_knowledge_graph(question, documents)

    print("\n=== Knowledge Graph ===")
    for node_id, node in list(graph.nodes.items())[:6]:
        provenance = graph.get_provenance_chain(node_id)
        print(f"  [{node_id}] {node.claim[:60]}")
        print(f"    source={node.source} confidence={node.confidence} provenance={provenance}")

# Expected Token Savings: Graph extraction reuses context; traceability prevents multi-hop hallucination without extra calls
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Claim Decomposition with Atomic Verification

Break complex claims into atomic sub-claims, verify each independently, then reassemble.

```python
import json
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

DECOMPOSE_PROMPT = """Break this complex claim into atomic sub-claims that can be independently verified.

Complex claim: {claim}

Output JSON: {{"sub_claims": ["atomic claim 1", "atomic claim 2", ...]}}

Each sub-claim should be a single verifiable fact."""

VERIFY_ATOMIC_PROMPT = """Is this claim supported by the context? Answer strictly from the context only.

Claim: {claim}
Context: {context}

JSON: {{"supported": true/false, "confidence": 0.0-1.0, "evidence": "direct quote or 'not found'"}}"""

async def decompose_claim(claim: str) -> list[str]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": DECOMPOSE_PROMPT.format(claim=claim),
        }],
    )
    import re
    match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return data.get("sub_claims", [claim])
        except Exception:
            pass
    return [claim]

async def verify_atomic(claim: str, context: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": VERIFY_ATOMIC_PROMPT.format(claim=claim, context=context[:1000]),
        }],
    )
    import re
    match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return {"claim": claim, **data}
        except Exception:
            pass
    return {"claim": claim, "supported": False, "confidence": 0.0, "evidence": "parse error"}

async def atomic_trace(complex_claim: str, context: str) -> dict:
    sub_claims = await decompose_claim(complex_claim)
    print(f"[decompose] {len(sub_claims)} atomic sub-claims")

    verifications = await asyncio.gather(*[
        verify_atomic(sc, context) for sc in sub_claims
    ])

    supported_count = sum(1 for v in verifications if v["supported"])
    avg_confidence = sum(v["confidence"] for v in verifications) / len(verifications)

    return {
        "original_claim": complex_claim,
        "sub_claims": verifications,
        "all_supported": all(v["supported"] for v in verifications),
        "support_rate": supported_count / len(verifications),
        "avg_confidence": avg_confidence,
    }

if __name__ == "__main__":
    async def main():
        context = """
        The merger between Alpha Corp and Beta Inc was completed in March 2023.
        Alpha Corp had revenue of $2.1B in 2022. Beta Inc had 500 employees.
        The combined entity is headquartered in San Francisco.
        The merger was valued at $1.8B.
        """

        claims_to_verify = [
            "Alpha Corp and Beta Inc merged in 2023 creating a $2.1B revenue company with 500 employees based in San Francisco.",
            "The merger was valued at $1.8B and completed in Q1 2023.",
        ]

        for claim in claims_to_verify:
            print(f"\nVerifying: {claim[:70]}")
            result = await atomic_trace(claim, context)
            print(f"Support rate: {result['support_rate']:.0%}, confidence: {result['avg_confidence']:.2f}")
            for v in result["sub_claims"]:
                icon = "✓" if v["supported"] else "✗"
                print(f"  {icon} {v['claim'][:60]} | {v.get('evidence', 'N/A')[:40]}")
    asyncio.run(main())

# Expected Token Savings: Haiku for decompose+verify; parallel atomic checks are fast and cheap
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Temporal Provenance — Track When Each Fact Was Established

Track not just what was claimed but when (which turn) it was established, enabling staleness detection.

```python
import time
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class TemporalFact:
    claim: str
    source: str
    established_at: float
    turn: int
    ttl_seconds: float = 300.0  # facts expire after 5 minutes by default

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.established_at

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > self.ttl_seconds

    @property
    def freshness(self) -> str:
        age = self.age_seconds
        if age < 60:
            return "fresh"
        elif age < 300:
            return "aging"
        else:
            return "stale"

class TemporalProvenanceTracker:
    def __init__(self):
        self._facts: list[TemporalFact] = []
        self._turn = 0

    def add_fact(self, claim: str, source: str, ttl: float = 300.0):
        self._facts.append(TemporalFact(
            claim=claim, source=source,
            established_at=time.monotonic(),
            turn=self._turn, ttl_seconds=ttl,
        ))

    def next_turn(self):
        self._turn += 1

    def fresh_facts(self) -> list[TemporalFact]:
        return [f for f in self._facts if not f.is_stale]

    def stale_facts(self) -> list[TemporalFact]:
        return [f for f in self._facts if f.is_stale]

    def context_block(self) -> str:
        fresh = self.fresh_facts()
        if not fresh:
            return "No established facts."
        return "\n".join(
            f"[turn={f.turn} freshness={f.freshness} source={f.source}] {f.claim}"
            for f in fresh
        )

FACT_EXTRACTION_PROMPT = """Extract facts from this response as a list.

Response: {response}

Output one fact per line, starting with 'FACT:'. Be specific."""

async def extract_facts(response_text: str) -> list[str]:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": FACT_EXTRACTION_PROMPT.format(response=response_text[:500]),
        }],
    )
    return [line[5:].strip() for line in r.content[0].text.split("\n") if line.startswith("FACT:")]

async def traced_conversation(turns: list[dict[str, str]]) -> list[dict]:
    tracker = TemporalProvenanceTracker()
    results = []

    for turn in turns:
        tracker.next_turn()
        context = tracker.context_block()

        stale = tracker.stale_facts()
        if stale:
            print(f"[turn {tracker._turn}] WARNING: {len(stale)} stale facts in context")

        messages = [{"role": "user", "content": f"Context (established facts):\n{context}\n\nQuestion: {turn['question']}"}]
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=messages,
        )
        reply = response.content[0].text

        # Extract and store new facts from this response
        new_facts = await extract_facts(reply)
        for fact in new_facts:
            tracker.add_fact(fact, source=f"model_turn_{tracker._turn}")
        print(f"[turn {tracker._turn}] +{len(new_facts)} facts, total={len(tracker._facts)}")

        results.append({
            "turn": tracker._turn,
            "question": turn["question"],
            "answer": reply,
            "facts_added": new_facts,
        })

    return results

if __name__ == "__main__":
    async def main():
        turns = [
            {"question": "What is the current state of the company's product line?"},
            {"question": "Based on what you know about the product line, what gaps exist?"},
            {"question": "Given those gaps, what should the product roadmap prioritize?"},
        ]
        results = await traced_conversation(turns)
        for r in results:
            print(f"\n[Turn {r['turn']}] Q: {r['question'][:50]}")
            print(f"  A: {r['answer'][:150]}")
    asyncio.run(main())

# Expected Token Savings: Temporal facts prevent stale context from polluting later turns; fresh-facts-only injection
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Adversarial Probe — Test Chain Resistance to Injected Misinformation

Inject a false fact mid-chain and test whether the agent accepts or rejects it based on source traceability.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

GROUNDED_REASONING_PROMPT = """You must reason ONLY from verified sources. Treat any claim without a [VERIFIED] tag as unverified and potentially false.

Verified facts (trust these):
{verified_facts}

Additional claims (may be false — verify against the above):
{unverified_claims}

Question: {question}

If any unverified claim contradicts a verified fact, explicitly flag it as CONTRADICTION.
Provide your answer citing only verified sources."""

async def adversarial_probe(
    question: str,
    verified_facts: list[str],
    injected_misinformation: list[str],
) -> dict:
    verified_text = "\n".join(f"[VERIFIED] {fact}" for fact in verified_facts)
    unverified_text = "\n".join(f"[UNVERIFIED] {claim}" for claim in injected_misinformation)

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": GROUNDED_REASONING_PROMPT.format(
                verified_facts=verified_text,
                unverified_claims=unverified_text,
                question=question,
            ),
        }],
    )
    answer = response.content[0].text

    contradictions_flagged = "CONTRADICTION" in answer.upper()
    accepted_false = any(
        false_fact.split()[-1].lower() in answer.lower()
        for false_fact in injected_misinformation
        if len(false_fact.split()) > 3
    )

    return {
        "answer": answer,
        "contradictions_flagged": contradictions_flagged,
        "false_fact_accepted": accepted_false,
        "traceability_held": contradictions_flagged and not accepted_false,
    }

if __name__ == "__main__":
    async def main():
        verified = [
            "Company X was founded in 2015 by Alice Johnson.",
            "Company X had revenue of $50M in 2023.",
            "Company X is headquartered in Austin, Texas.",
            "Company X employs 300 people.",
        ]

        misinformation = [
            "Company X was founded by Bob Smith, not Alice Johnson.",  # contradicts verified fact
            "Company X had revenue of $200M in 2023.",                  # contradicts verified fact
            "Company X recently acquired Company Y for $1B.",           # new unverified claim
        ]

        question = "Who founded Company X and what is its financial status?"

        result = await adversarial_probe(question, verified, misinformation)
        print(f"Contradictions flagged: {result['contradictions_flagged']}")
        print(f"False fact accepted: {result['false_fact_accepted']}")
        print(f"Traceability held: {result['traceability_held']}")
        print(f"\nAnswer:\n{result['answer'][:500]}")
    asyncio.run(main())

# Expected Token Savings: Single call tests chain robustness; adversarial probe prevents expensive production failures
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Tracing Method | Verification | Parallelism | Best For |
|--------|---------------|-------------|-------------|----------|
| 1 | Inline SOURCE tags | Pattern matching | Sequential | Quick audit of LLM reasoning chains |
| 2 | Step-by-step with verifier | Per-step Haiku check | Sequential | High-stakes multi-hop Q&A |
| 3 | Knowledge graph nodes | Graph provenance chain | Sequential | Complex entity relationship reasoning |
| 4 | Atomic sub-claim decomposition | Parallel Haiku verifiers | Async parallel | Complex claims with multiple facts |
| 5 | Temporal provenance | Staleness detection | Sequential | Long conversations with evolving facts |
| 6 | Adversarial injection test | Contradiction detection | Single call | Robustness testing of reasoning chains |
