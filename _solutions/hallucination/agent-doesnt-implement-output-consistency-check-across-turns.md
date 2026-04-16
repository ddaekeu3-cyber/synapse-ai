---
layout: solution
title: "Agent Doesn't Implement Output Consistency Check Across Turns"
category: hallucination
description: "Detect when an agent contradicts its own prior statements within a conversation—flagging self-inconsistencies before they undermine user trust or cause downstream decisions to be based on conflicting information."
tags: [consistency, self-contradiction, multi-turn, fact-tracking, hallucination]
---

# Agent Doesn't Implement Output Consistency Check Across Turns

## Problem

Agents frequently contradict themselves across turns: stating a price is $100 in turn 2 and $120 in turn 7, recommending approach A then advising against it three turns later, or claiming incompatible facts. Without tracking, contradictions accumulate silently and users can't trust any specific claim.

## Solution Options

### Option 1: Claim Ledger with Contradiction Detection

```python
import anthropic
import re
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ClaimLedger:
    claims: list[dict] = field(default_factory=list)

    def add(self, turn: int, claim: str) -> None:
        self.claims.append({"turn": turn, "claim": claim})

    def get_context(self, max_claims: int = 10) -> str:
        if not self.claims:
            return ""
        recent = self.claims[-max_claims:]
        return "\n".join(f"Turn {c['turn']}: {c['claim']}" for c in recent)

def extract_claims(text: str) -> list[str]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Extract all concrete factual claims as a bulleted list. One per line, starting with '-':\n{text}"
        }]
    )
    lines = resp.content[0].text.strip().split("\n")
    return [l.lstrip("- •").strip() for l in lines if l.strip() and l.strip().startswith("-")]

def check_consistency(new_claim: str, ledger: ClaimLedger) -> tuple[bool, str]:
    if not ledger.claims:
        return True, ""
    prior_context = ledger.get_context()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"""Does this new claim contradict any prior claim?

Prior claims:
{prior_context}

New claim: "{new_claim}"

Answer: CONSISTENT or CONTRADICTION: <explanation>"""
        }]
    )
    text = resp.content[0].text.strip()
    if text.upper().startswith("CONTRADICTION"):
        return False, text.replace("CONTRADICTION:", "").strip()
    return True, ""

def consistent_chat(messages: list[dict], system: str, user_input: str,
                     ledger: ClaimLedger, turn: int) -> tuple[str, list[str]]:
    messages.append({"role": "user", "content": user_input})
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=384,
        system=system,
        messages=messages
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    new_claims = extract_claims(reply)
    contradictions = []
    for claim in new_claims:
        consistent, reason = check_consistency(claim, ledger)
        if not consistent:
            contradictions.append(f"CONTRADICTION: '{claim[:50]}' — {reason[:80]}")
        ledger.add(turn, claim)

    return reply, contradictions

system = "You are a technical advisor for a software team."
messages = []
ledger = ClaimLedger()

conversation = [
    "What programming language should we use for our backend?",
    "Should we use Python or Go?",
    "We decided to use Python. Is that the right choice?",
    "Actually, is Go much faster than Python for this use case?",
]

for turn, user_msg in enumerate(conversation, 1):
    reply, contradictions = consistent_chat(messages, system, user_msg, ledger, turn)
    print(f"\n[Turn {turn}] Q: {user_msg}")
    print(f"A: {reply[:100]}...")
    if contradictions:
        for c in contradictions:
            print(f"  ⚠️  {c}")

# Expected Token Savings: claim extraction uses haiku (~30 tokens); prevents costly trust-repair conversations
# Environment: technical advisors, medical Q&A, any consistency-critical multi-turn agent
```

### Option 2: Structured Fact Store with Semantic Conflict Detection

```python
import anthropic
import json
import re
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class Fact:
    subject: str
    predicate: str
    value: str
    turn: int
    confidence: float = 1.0

@dataclass
class FactStore:
    facts: list[Fact] = field(default_factory=list)

    def add(self, fact: Fact) -> None:
        self.facts.append(fact)

    def find_conflicts(self, new_fact: Fact) -> list[Fact]:
        """Find facts with same subject+predicate but different value."""
        return [
            f for f in self.facts
            if f.subject.lower() == new_fact.subject.lower()
            and f.predicate.lower() == new_fact.predicate.lower()
            and f.value.lower() != new_fact.value.lower()
        ]

    def to_context(self) -> str:
        if not self.facts:
            return "No established facts yet."
        return "\n".join(
            f"[T{f.turn}] {f.subject} {f.predicate}: {f.value}"
            for f in self.facts[-12:]
        )

def extract_structured_facts(text: str, turn: int) -> list[Fact]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Extract factual statements as JSON array.
Format: [{{"subject": "...", "predicate": "is|costs|requires|supports|has", "value": "..."}}]
Return [] if no concrete facts.

Text: {text[:500]}"""
        }]
    )
    text_out = resp.content[0].text
    match = re.search(r'\[.*?\]', text_out, re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group())
        return [Fact(d["subject"], d["predicate"], d["value"], turn) for d in raw
                if all(k in d for k in ["subject", "predicate", "value"])]
    except (json.JSONDecodeError, KeyError):
        return []

store = FactStore()
messages = []

system = "You are a knowledgeable product advisor. Be precise and consistent."

turns = [
    "What is the price of the Pro plan?",
    "What features does the Pro plan include?",
    "Is the Pro plan $49 or $59 per month?",  # designed to potentially trigger contradiction
    "Does the Pro plan support SSO?",
]

for turn_num, user_msg in enumerate(turns, 1):
    messages.append({"role": "user", "content": user_msg})
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    new_facts = extract_structured_facts(reply, turn_num)
    conflicts_found = []
    for fact in new_facts:
        conflicts = store.find_conflicts(fact)
        if conflicts:
            for conflict in conflicts:
                conflicts_found.append(f"'{fact.subject} {fact.predicate}': was '{conflict.value}' (T{conflict.turn}), now '{fact.value}'")
        store.add(fact)

    print(f"\n[T{turn_num}] {user_msg}")
    print(f"  Reply: {reply[:80]}...")
    print(f"  Facts extracted: {len(new_facts)}")
    if conflicts_found:
        for c in conflicts_found:
            print(f"  ⚠️  CONFLICT: {c}")

print(f"\nFact store ({len(store.facts)} facts):")
print(store.to_context())

# Expected Token Savings: structured extraction replaces expensive full-text comparison; ~40% cheaper
# Environment: product advisory bots, pricing assistants, spec-heavy technical advisors
```

### Option 3: Sliding Window Similarity Check for Near-Contradictions

```python
import anthropic
import re
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class StatementRecord:
    turn: int
    text: str
    keywords: frozenset

def extract_keywords(text: str) -> frozenset:
    words = re.findall(r'\b[a-z]{4,}\b', text.lower())
    stopwords = {'that', 'this', 'with', 'from', 'have', 'will', 'been', 'were',
                 'they', 'their', 'what', 'when', 'which', 'each', 'also', 'just'}
    return frozenset(w for w in words if w not in stopwords)

def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def check_near_contradiction(new_stmt: StatementRecord,
                              history: list[StatementRecord],
                              similarity_threshold: float = 0.4) -> list[str]:
    """Flag pairs with high keyword overlap but potentially conflicting content."""
    flagged = []
    for prior in history:
        sim = jaccard(new_stmt.keywords, prior.keywords)
        if sim >= similarity_threshold:
            # Topics overlap — check semantic consistency
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{
                    "role": "user",
                    "content": f"""Do these two statements contradict each other?
A (turn {prior.turn}): "{prior.text[:150]}"
B (turn {new_stmt.turn}): "{new_stmt.text[:150]}"

Answer: CONSISTENT or CONTRADICTION: <reason>"""
                }]
            )
            verdict = resp.content[0].text.strip()
            if verdict.upper().startswith("CONTRADICTION"):
                reason = verdict.split(":", 1)[-1].strip() if ":" in verdict else verdict
                flagged.append(f"Turn {prior.turn} vs {new_stmt.turn} (sim={sim:.2f}): {reason[:80]}")
    return flagged

history: list[StatementRecord] = []
messages = []

for turn, user_msg in enumerate([
    "What database should I use for a high-write workload?",
    "What are the tradeoffs of using Cassandra?",
    "Should I use PostgreSQL for high-write workloads?",  # potentially contradictory
    "Can you summarize your database recommendations?"
], 1):
    messages.append({"role": "user", "content": user_msg})
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=messages
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    stmt = StatementRecord(turn=turn, text=reply, keywords=extract_keywords(reply))
    contradictions = check_near_contradiction(stmt, history)
    history.append(stmt)

    print(f"\n[Turn {turn}] {user_msg[:50]}")
    print(f"  {reply[:80]}...")
    if contradictions:
        for c in contradictions:
            print(f"  ⚠️  {c}")
    else:
        print(f"  ✓ Consistent with prior {len(history)-1} statements")

# Expected Token Savings: similarity filter means only ~20% of pairs need LLM consistency check
# Environment: advisory agents, recommendation systems, any long technical consultation
```

### Option 4: LLM-as-Consistency-Auditor with Per-Turn Audit

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class AuditLog:
    turns: list[dict] = field(default_factory=list)
    MAX_AUDIT_HISTORY = 8

    def add(self, turn: int, user: str, assistant: str) -> None:
        self.turns.append({"turn": turn, "user": user, "assistant": assistant})

    def get_audit_context(self) -> str:
        recent = self.turns[-self.MAX_AUDIT_HISTORY:]
        return "\n\n".join(
            f"Turn {t['turn']}\nUser: {t['user'][:100]}\nAssistant: {t['assistant'][:200]}"
            for t in recent
        )

def audit_new_response(new_response: str, audit_log: AuditLog, turn: int) -> dict:
    """Run a dedicated consistency audit comparing new response against history."""
    if len(audit_log.turns) < 2:
        return {"consistent": True, "issues": []}

    context = audit_log.get_audit_context()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a strict consistency auditor. Identify only genuine factual contradictions, not mere topic changes or elaborations.",
        messages=[{
            "role": "user",
            "content": f"""Audit this new response for contradictions with prior conversation.

=== PRIOR CONVERSATION ===
{context}

=== NEW RESPONSE (Turn {turn}) ===
{new_response}

Identify any direct contradictions. Format:
CONSISTENT — if no contradictions found
CONTRADICTION #1: <description>
CONTRADICTION #2: <description>
(only list genuine factual conflicts)"""
        }]
    )
    text = resp.content[0].text.strip()
    issues = []
    if not text.upper().startswith("CONSISTENT"):
        for line in text.split("\n"):
            if line.strip().upper().startswith("CONTRADICTION"):
                issues.append(line.split(":", 1)[-1].strip() if ":" in line else line)
    return {"consistent": len(issues) == 0, "issues": issues, "audit_text": text}

audit_log = AuditLog()
messages = []
system = "You are a software architecture consultant."

for turn, user_msg in enumerate([
    "What is the best architecture for our startup's backend?",
    "How many microservices should we start with?",
    "Should we use a monolith or microservices to start?",  # potential contradiction
    "What database do you recommend?",
    "Is a monolith approach viable at our scale?"
], 1):
    messages.append({"role": "user", "content": user_msg})
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    audit = audit_new_response(reply, audit_log, turn)
    audit_log.add(turn, user_msg, reply)

    consistent_marker = "✓" if audit["consistent"] else "⚠️"
    print(f"\n{consistent_marker} [Turn {turn}] {user_msg[:50]}")
    print(f"   {reply[:80]}...")
    if audit["issues"]:
        for issue in audit["issues"]:
            print(f"   ISSUE: {issue}")

# Expected Token Savings: dedicated haiku auditor is cheaper than re-asking sonnet to self-correct
# Environment: architecture advisors, compliance agents, multi-session consistency tracking
```

### Option 5: Contradiction-Aware System Prompt Injection

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ConsistencyContext:
    established_positions: list[str] = field(default_factory=list)
    MAX_POSITIONS = 8

    def add_position(self, position: str) -> None:
        if len(self.established_positions) >= self.MAX_POSITIONS:
            self.established_positions.pop(0)
        self.established_positions.append(position)

    def consistency_block(self) -> str:
        if not self.established_positions:
            return ""
        positions = "\n".join(f"- {p}" for p in self.established_positions)
        return (
            f"\n\nESTABLISHED POSITIONS (you MUST remain consistent with these):\n"
            f"{positions}\n"
            "If asked something that seems to contradict a prior position, acknowledge the tension and resolve it explicitly."
        )

def extract_key_position(response: str) -> str | None:
    """Extract the main recommendation or factual claim from a response."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"Extract the single most important recommendation or factual claim from this text. One sentence max:\n{response[:400]}"
        }]
    )
    position = resp.content[0].text.strip()
    return position if len(position) > 10 else None

BASE_SYSTEM = "You are an expert technical consultant. Always be consistent with your prior advice."

ctx = ConsistencyContext()
messages = []

for user_msg in [
    "What tech stack should our startup use?",
    "Should we use a microservices architecture?",
    "What about a monolith instead of microservices?",
    "What database should we use?",
    "On reflection, should we have started with a monolith?"
]:
    system = BASE_SYSTEM + ctx.consistency_block()
    messages.append({"role": "user", "content": user_msg})
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    position = extract_key_position(reply)
    if position:
        ctx.add_position(position)

    print(f"Q: {user_msg[:55]}")
    print(f"A: {reply[:100]}...")
    print(f"   [Tracked: {position[:60] if position else 'none'}]\n")

# Expected Token Savings: proactive injection costs ~50 tokens but prevents expensive correction turns
# Environment: recommendation agents, consulting bots, decision-support assistants
```

### Option 6: Multi-Turn Consistency Score with Trend Alerting

```python
import anthropic
import statistics
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ConsistencyScore:
    turn: int
    score: float  # 1.0 = fully consistent, 0.0 = full contradiction
    issues: list[str]

@dataclass
class ConsistencyMonitor:
    scores: list[ConsistencyScore] = field(default_factory=list)
    ALERT_THRESHOLD = 0.5
    TREND_WINDOW = 3

    def add(self, score: ConsistencyScore) -> None:
        self.scores.append(score)

    def is_trending_down(self) -> bool:
        if len(self.scores) < self.TREND_WINDOW:
            return False
        recent = [s.score for s in self.scores[-self.TREND_WINDOW:]]
        return all(recent[i] > recent[i+1] for i in range(len(recent)-1))

    def avg_score(self) -> float:
        if not self.scores:
            return 1.0
        return statistics.mean(s.score for s in self.scores)

    def report(self) -> None:
        print(f"\n=== Consistency Report ({len(self.scores)} turns) ===")
        print(f"Average score: {self.avg_score():.2f}")
        print(f"Trending down: {self.is_trending_down()}")
        for s in self.scores:
            bar = "█" * int(s.score * 10) + "░" * (10 - int(s.score * 10))
            issues = f" [{'; '.join(s.issues[:1])}]" if s.issues else ""
            print(f"  T{s.turn}: [{bar}] {s.score:.2f}{issues}")

def score_consistency(new_response: str, history: list[dict]) -> ConsistencyScore:
    turn = len(history) // 2 + 1
    if len(history) < 4:
        return ConsistencyScore(turn=turn, score=1.0, issues=[])

    prior_responses = "\n\n".join(
        f"T{i+1}: {m['content'][:200]}"
        for i, m in enumerate(history)
        if m["role"] == "assistant"
    )[-800:]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"""Score consistency of new response with prior responses.

Prior responses:
{prior_responses}

New response: {new_response[:300]}

Score 0.0-1.0 (1.0=fully consistent). List any contradictions.
Format: SCORE: 0.X\nISSUES: issue1 | issue2 (or NONE)"""
        }]
    )
    text = resp.content[0].text
    score = 1.0
    issues = []
    for line in text.split("\n"):
        if line.startswith("SCORE:"):
            import re
            m = re.search(r'[\d.]+', line)
            if m:
                score = float(m.group())
        elif line.startswith("ISSUES:") and "NONE" not in line.upper():
            issues = [i.strip() for i in line.replace("ISSUES:", "").split("|") if i.strip()]
    return ConsistencyScore(turn=turn, score=score, issues=issues)

monitor = ConsistencyMonitor()
messages = []
system = "You are a product strategy advisor."

for user_msg in [
    "Should we build for enterprise or consumer?",
    "What pricing model do you recommend?",
    "Should we go freemium?",
    "Would a pure enterprise model be better?",
    "So should we go consumer or enterprise?"
]:
    messages.append({"role": "user", "content": user_msg})
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=messages
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    cs = score_consistency(reply, messages[:-2])
    monitor.add(cs)
    alert = " [ALERT]" if cs.score < monitor.ALERT_THRESHOLD else ""
    print(f"T{cs.turn}: score={cs.score:.2f}{alert} | {user_msg[:45]}")

monitor.report()
if monitor.is_trending_down():
    print("\n⚠️  TREND ALERT: Consistency declining — agent may be drifting.")

# Expected Token Savings: trend alerting enables early intervention before cascading contradictions
# Environment: long advisory sessions, strategic planning agents, consistency SLO monitoring
```

## Comparison

| Option | Detection Method | Real-time | Corrective Action | Best For |
|--------|-----------------|-----------|-------------------|----------|
| 1 | Claim ledger + LLM check | Yes | Manual review | General multi-turn agents |
| 2 | Structured fact store | Yes | Contradiction log | Product advisors, pricing bots |
| 3 | Keyword similarity filter | Yes | None | High-volume consistency checks |
| 4 | Dedicated LLM auditor | Per-turn | Audit log | Compliance, high-stakes advice |
| 5 | Proactive system injection | Per-turn | Prevention | Recommendation agents |
| 6 | Consistency score trend | Per-turn | Trend alerting | Quality monitoring dashboards |
