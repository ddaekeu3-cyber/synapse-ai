---
layout: solution
title: "Agent Doesn't Implement Structured Fact-Checking Pipeline"
category: hallucination
description: "Extract discrete factual claims from agent responses, verify each claim independently, and return a structured verdict with confidence scores—catching hallucinations before they reach users."
tags: [fact-checking, hallucination, verification, claims, grounding]
---

# Agent Doesn't Implement Structured Fact-Checking Pipeline

## Problem

Agent responses contain a mix of true facts, plausible-sounding hallucinations, and genuine uncertainty—but they're presented uniformly as confident assertions. Without a fact-checking pipeline, downstream systems and users can't distinguish verified claims from fabrications.

## Solution Options

### Option 1: Claim Extraction → Verification Two-Stage Pipeline

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class Claim:
    text: str
    verdict: str = "unverified"  # verified / refuted / uncertain
    confidence: float = 0.0
    reasoning: str = ""

def extract_claims(response_text: str) -> list[str]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Extract all discrete factual claims from this text.
Each claim should be a single verifiable statement.
Return as JSON array: ["claim1", "claim2", ...]

Text: {response_text}"""
        }]
    )
    text = resp.content[0].text
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return []

def verify_claim(claim: str) -> Claim:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Verify this factual claim based on your knowledge:
"{claim}"

Respond with JSON:
{{"verdict": "verified|refuted|uncertain", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""
        }]
    )
    text = resp.content[0].text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        data = json.loads(match.group())
        return Claim(
            text=claim,
            verdict=data.get("verdict", "uncertain"),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning", "")
        )
    return Claim(text=claim, verdict="uncertain", confidence=0.5)

def fact_check_response(response: str) -> dict:
    claims = extract_claims(response)
    print(f"Extracted {len(claims)} claims")
    verified_claims = [verify_claim(c) for c in claims]

    refuted = [c for c in verified_claims if c.verdict == "refuted"]
    uncertain = [c for c in verified_claims if c.verdict == "uncertain"]
    verified = [c for c in verified_claims if c.verdict == "verified"]

    return {
        "original_response": response,
        "total_claims": len(claims),
        "verified": len(verified),
        "refuted": len(refuted),
        "uncertain": len(uncertain),
        "overall_reliability": round(len(verified) / max(len(claims), 1), 2),
        "flagged_claims": [{"text": c.text, "reason": c.reasoning} for c in refuted]
    }

# Test
response = """Python was created by Guido van Rossum and first released in 1991.
The language emphasizes code readability with significant whitespace.
Python 2 was officially discontinued in 2020. The CPython implementation
is written in Java. Python is widely used for machine learning and data science."""

result = fact_check_response(response)
print(f"Reliability: {result['overall_reliability']:.0%}")
print(f"Flagged: {result['flagged_claims']}")

# Expected Token Savings: haiku for extraction (~50 tokens), sonnet for verification (targeted)
# Environment: knowledge-intensive agents, medical/legal Q&A, educational assistants
```

### Option 2: Self-Consistency Fact Checker with Multiple Samples

```python
import anthropic
import re
from dataclasses import dataclass
from collections import Counter

client = anthropic.Anthropic()

@dataclass
class ConsistencyResult:
    claim: str
    answers: list[str]
    majority_answer: str
    consistency_score: float  # 1.0 = all agree, 0.0 = all disagree
    is_reliable: bool

def sample_answer(claim: str, n_samples: int = 3) -> list[str]:
    """Ask the same question N times and check for consistency."""
    answers = []
    for i in range(n_samples):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": f"Is this statement true or false? Answer in one word (True/False/Uncertain):\n{claim}"
            }]
        )
        raw = resp.content[0].text.strip().lower()
        if "true" in raw:
            answers.append("true")
        elif "false" in raw:
            answers.append("false")
        else:
            answers.append("uncertain")
    return answers

def check_consistency(claim: str, n_samples: int = 3, threshold: float = 0.67) -> ConsistencyResult:
    answers = sample_answer(claim, n_samples)
    counts = Counter(answers)
    majority_answer, majority_count = counts.most_common(1)[0]
    consistency_score = majority_count / n_samples
    return ConsistencyResult(
        claim=claim,
        answers=answers,
        majority_answer=majority_answer,
        consistency_score=consistency_score,
        is_reliable=consistency_score >= threshold and majority_answer != "uncertain"
    )

# Test claims
claims = [
    "Python was first released in 1991.",
    "The Great Wall of China is visible from space with the naked eye.",
    "HTTP uses port 80 by default.",
    "JavaScript was created in 10 days.",
    "SQL stands for Structured Query Language.",
]

for claim in claims:
    result = check_consistency(claim, n_samples=3)
    flag = "" if result.is_reliable else " [UNRELIABLE]"
    print(f"{claim[:60]}")
    print(f"  answers={result.answers} majority={result.majority_answer} score={result.consistency_score:.2f}{flag}\n")

# Expected Token Savings: haiku x3 per claim cheaper than sonnet x1 for verification
# Environment: high-stakes factual Q&A, trivia agents, knowledge base validation
```

### Option 3: Grounded Fact Checker with Reference Context

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class GroundedVerdict:
    claim: str
    verdict: str
    supporting_quote: str
    confidence: float

def extract_and_verify_against_source(response: str, source_text: str) -> list[GroundedVerdict]:
    """Verify claims in response against a provided source document."""
    extraction_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"List all factual claims from this text, one per line:\n{response}"
        }]
    )
    claims = [l.strip() for l in extraction_resp.content[0].text.strip().split("\n")
              if l.strip() and not l.strip().startswith("#")]

    verdicts = []
    for claim in claims[:6]:  # limit to 6 claims
        verify_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="You are a precise fact-checker. Only judge claims against the provided source text.",
            messages=[{
                "role": "user",
                "content": f"""Source document:
{source_text[:1000]}

Claim to verify: "{claim}"

Answer:
VERDICT: supported|contradicted|not_mentioned
QUOTE: exact quote from source that supports/contradicts (or "N/A")
CONFIDENCE: 0.0-1.0"""
            }]
        )
        text = verify_resp.content[0].text
        verdict = "not_mentioned"
        quote = "N/A"
        confidence = 0.5

        for line in text.split("\n"):
            if line.startswith("VERDICT:"):
                verdict = line.replace("VERDICT:", "").strip().lower()
            elif line.startswith("QUOTE:"):
                quote = line.replace("QUOTE:", "").strip()
            elif line.startswith("CONFIDENCE:"):
                try:
                    confidence = float(re.search(r'[\d.]+', line).group())
                except:
                    pass

        verdicts.append(GroundedVerdict(claim=claim, verdict=verdict,
                                         supporting_quote=quote, confidence=confidence))

    return verdicts

SOURCE = """Python is a high-level programming language created by Guido van Rossum.
It was first released in 1991. Python's design philosophy emphasizes code readability.
Python 2 reached end-of-life on January 1, 2020. CPython is the reference implementation,
written in C, not Java. Python supports multiple programming paradigms."""

AGENT_RESPONSE = """Python was created by Guido van Rossum in 1991. The language is known
for readable code. CPython is written in Java. Python 2 was discontinued in 2020."""

verdicts = extract_and_verify_against_source(AGENT_RESPONSE, SOURCE)
for v in verdicts:
    flag = "[WARN]" if v.verdict == "contradicted" else "[OK]" if v.verdict == "supported" else "[?]"
    print(f"{flag} '{v.claim[:60]}' -> {v.verdict} (conf={v.confidence:.2f})")

# Expected Token Savings: grounding against source avoids expensive open-ended model knowledge lookup
# Environment: RAG systems, document Q&A, research assistants with citations
```

### Option 4: Confidence-Gated Response with Automatic Hedging

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class HedgedResponse:
    original: str
    hedged: str
    low_confidence_claims: list[str]
    overall_confidence: float
    should_warn_user: bool

def score_response_confidence(response: str, topic: str) -> dict:
    """Score confidence across multiple dimensions."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Analyze this response about "{topic}" for factual reliability.

Response: {response}

Score each dimension 0.0-1.0:
{{"factual_accuracy": 0.X, "specificity_risk": 0.X, "temporal_risk": 0.X, "domain_confidence": 0.X, "low_confidence_phrases": ["phrase1"]}}

factual_accuracy: how likely the facts are correct
specificity_risk: risk from specific numbers/dates that could be wrong
temporal_risk: risk from time-sensitive information
domain_confidence: how well-established is this topic"""
        }]
    )
    match = re.search(r'\{.*\}', resp.content[0].text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"factual_accuracy": 0.7, "specificity_risk": 0.3, "temporal_risk": 0.3,
            "domain_confidence": 0.7, "low_confidence_phrases": []}

def add_hedges(response: str, low_confidence_phrases: list[str]) -> str:
    """Insert hedging language around low-confidence claims."""
    hedged = response
    hedge_map = {
        "is": "is believed to be",
        "was": "is reported to have been",
        "will": "may",
        "always": "generally",
        "never": "rarely",
        "all": "most",
    }
    for phrase in low_confidence_phrases:
        if phrase in hedged:
            hedged = hedged.replace(phrase, f"*{phrase}*", 1)  # italicize for emphasis
    return hedged

def confidence_gated_response(user_query: str, confidence_threshold: float = 0.75) -> HedgedResponse:
    # Generate initial response
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=384,
        messages=[{"role": "user", "content": user_query}]
    )
    original = resp.content[0].text

    # Score it
    topic = user_query[:50]
    scores = score_response_confidence(original, topic)
    overall = (scores.get("factual_accuracy", 0.7) * 0.5 +
               (1 - scores.get("specificity_risk", 0.3)) * 0.3 +
               scores.get("domain_confidence", 0.7) * 0.2)
    low_conf_phrases = scores.get("low_confidence_phrases", [])

    if overall < confidence_threshold:
        hedged = add_hedges(original, low_conf_phrases)
        disclaimer = "\n\n*Note: Some details in this response may benefit from verification.*"
        hedged += disclaimer
    else:
        hedged = original

    return HedgedResponse(
        original=original,
        hedged=hedged,
        low_confidence_claims=low_conf_phrases,
        overall_confidence=round(overall, 2),
        should_warn_user=overall < confidence_threshold
    )

queries = [
    "What is the exact market cap of Apple as of today?",
    "What is a binary search tree?",
    "Who won the 2024 presidential election in France?"
]

for q in queries:
    result = confidence_gated_response(q)
    warn = " [USER WARNED]" if result.should_warn_user else ""
    print(f"Q: {q[:60]}")
    print(f"  Confidence: {result.overall_confidence:.0%}{warn}")
    print(f"  Low-conf phrases: {result.low_confidence_claims[:3]}\n")

# Expected Token Savings: scoring uses haiku; hedging is free; avoids expensive external verification
# Environment: user-facing chatbots, knowledge assistants, time-sensitive domain Q&A
```

### Option 5: Multi-Agent Debate Fact Checker

```python
import anthropic
import asyncio
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class DebateResult:
    claim: str
    proponent_arg: str
    opponent_arg: str
    judge_verdict: str
    final_confidence: float

async def get_proponent_argument(claim: str) -> str:
    """Agent that argues the claim is true."""
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system="You argue that the given statement is TRUE. Be specific and cite evidence.",
        messages=[{"role": "user", "content": f"Argue that this is true: {claim}"}]
    )
    return resp.content[0].text

async def get_opponent_argument(claim: str) -> str:
    """Agent that argues the claim is false or uncertain."""
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system="You argue that the given statement is FALSE or UNCERTAIN. Be specific and cite counter-evidence.",
        messages=[{"role": "user", "content": f"Argue against this: {claim}"}]
    )
    return resp.content[0].text

async def judge_debate(claim: str, pro: str, con: str) -> tuple[str, float]:
    """Neutral judge evaluates both sides."""
    resp = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system="You are a neutral fact-checker judge. Evaluate arguments fairly based on accuracy and evidence.",
        messages=[{
            "role": "user",
            "content": f"""Claim: "{claim}"

FOR: {pro}

AGAINST: {con}

Verdict: TRUE / FALSE / UNCERTAIN
Confidence: 0.0-1.0
Reasoning: one sentence

Format: VERDICT: X\nCONFIDENCE: X\nREASONING: X"""
        }]
    )
    text = resp.content[0].text
    verdict = "uncertain"
    confidence = 0.5
    for line in text.split("\n"):
        if line.startswith("VERDICT:"):
            v = line.replace("VERDICT:", "").strip().lower()
            verdict = v if v in ["true", "false", "uncertain"] else "uncertain"
        elif line.startswith("CONFIDENCE:"):
            import re
            m = re.search(r'[\d.]+', line)
            if m:
                confidence = float(m.group())
    return verdict, confidence

async def debate_fact_check(claims: list[str]) -> list[DebateResult]:
    results = []
    for claim in claims:
        pro_task = asyncio.create_task(get_proponent_argument(claim))
        con_task = asyncio.create_task(get_opponent_argument(claim))
        pro, con = await asyncio.gather(pro_task, con_task)
        verdict, confidence = await judge_debate(claim, pro, con)
        results.append(DebateResult(
            claim=claim, proponent_arg=pro[:100], opponent_arg=con[:100],
            judge_verdict=verdict, final_confidence=confidence
        ))
        print(f"'{claim[:50]}' -> {verdict} ({confidence:.0%})")
    return results

claims = [
    "The moon landing happened in 1969.",
    "Python is faster than C for numerical computation.",
    "HTTP/2 supports multiplexing.",
]
asyncio.run(debate_fact_check(claims))

# Expected Token Savings: parallel pro/con debate cuts wall time 50%; haiku agents, sonnet judge only
# Environment: high-stakes fact verification, adversarial claim testing, educational debate tools
```

### Option 6: Incremental Fact-Check with Early Rejection

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Iterator

client = anthropic.Anthropic()

@dataclass
class StreamingFactCheck:
    sentence_buffer: str = ""
    flagged_sentences: list[str] = field(default_factory=list)
    verified_count: int = 0
    total_checked: int = 0

    def is_factual_sentence(self, sentence: str) -> bool:
        """Quick heuristic: does sentence contain a specific factual claim?"""
        factual_indicators = [
            r'\bin \d{4}\b',           # year references
            r'\b\d+\s*(percent|%)\b',  # percentages
            r'\b(was|were|is|are) (founded|created|born|invented|released)\b',
            r'\b(first|last|largest|smallest|fastest|slowest)\b',
        ]
        return any(re.search(p, sentence, re.IGNORECASE) for p in factual_indicators)

    def quick_verify(self, sentence: str) -> tuple[str, float]:
        """Fast haiku-based single-sentence verification."""
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": f"True, False, or Uncertain? Answer in one word then confidence 0-100:\n{sentence}"
            }]
        )
        text = resp.content[0].text.strip().lower()
        verdict = "uncertain"
        confidence = 0.5
        if text.startswith("true"):
            verdict = "true"
        elif text.startswith("false"):
            verdict = "false"
        m = re.search(r'\d+', text)
        if m:
            confidence = min(int(m.group()), 100) / 100
        return verdict, confidence

    def process_sentence(self, sentence: str) -> dict | None:
        """Check a sentence and return verdict if it's factual."""
        sentence = sentence.strip()
        if len(sentence) < 20 or not self.is_factual_sentence(sentence):
            return None
        self.total_checked += 1
        verdict, confidence = self.quick_verify(sentence)
        if verdict == "false" and confidence > 0.7:
            self.flagged_sentences.append(sentence)
            return {"sentence": sentence, "verdict": verdict, "confidence": confidence, "action": "FLAG"}
        elif verdict == "true":
            self.verified_count += 1
        return {"sentence": sentence, "verdict": verdict, "confidence": confidence, "action": "OK"}

def stream_with_fact_check(prompt: str) -> str:
    checker = StreamingFactCheck()
    full_response = ""
    sentence_buffer = ""

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for chunk in stream.text_stream:
            full_response += chunk
            sentence_buffer += chunk
            # Check for sentence boundaries
            while any(p in sentence_buffer for p in ['. ', '! ', '? ', '.\n']):
                for delim in ['. ', '! ', '? ', '.\n']:
                    if delim in sentence_buffer:
                        parts = sentence_buffer.split(delim, 1)
                        sentence = parts[0] + delim[0]
                        sentence_buffer = parts[1]
                        result = checker.process_sentence(sentence)
                        if result:
                            action = result["action"]
                            flag = "[FLAG]" if action == "FLAG" else "[OK]"
                            print(f"  {flag} '{sentence[:60]}' -> {result['verdict']} ({result['confidence']:.0%})")
                        break

    print(f"\nSummary: checked={checker.total_checked} verified={checker.verified_count} flagged={len(checker.flagged_sentences)}")
    return full_response

stream_with_fact_check(
    "Tell me about the history of Python programming language, including key dates and facts."
)

# Expected Token Savings: incremental checking catches errors early; ~30% cheaper than post-hoc full check
# Environment: streaming agents with live fact verification, real-time content moderation
```

## Comparison

| Option | Approach | Speed | Accuracy | Best For |
|--------|----------|-------|----------|----------|
| 1 | Extract → verify pipeline | Medium | High | General fact-checking |
| 2 | Self-consistency sampling | Slow | Medium | High-stakes claims |
| 3 | Source-grounded verification | Fast | High | RAG/document Q&A |
| 4 | Confidence scoring + hedging | Fast | Medium | User-facing confidence |
| 5 | Multi-agent debate | Medium | High | Adversarial verification |
| 6 | Streaming incremental check | Fast | Medium | Real-time pipelines |
