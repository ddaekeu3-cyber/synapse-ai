---
layout: solution
title: "Agent Oscillates Between Two Contradictory Plans"
category: loop-stuck
description: "Agent alternates between two incompatible approaches turn after turn, unable to commit to either — generating contradictory work without making real progress."
tags: [loop-stuck, oscillation, planning, decision-making, indecision, infinite-loop]
---

## Symptom

Agent flip-flops between two strategies indefinitely:

```
Turn 1: "I'll use a microservices architecture with separate databases per service."
Turn 2: "Actually, a monolith would be more appropriate for this team size. Let me redesign."
Turn 3: "On reflection, microservices provide better scalability. Reverting to microservices."
Turn 4: "The deployment complexity of microservices is prohibitive. Switching to monolith."
Turn 5: "After considering the growth trajectory, microservices are the right call..."
[Loop continues for 15 more turns, consuming the entire context window]

# Or in code generation:
Turn 1: Uses async/await throughout
Turn 2: "Synchronous code is simpler. Rewriting."
Turn 3: "Async is needed for performance. Reverting."
Turn 4: "Sync is more readable. Switching back."
```

The agent can articulate valid reasons for each option but cannot synthesize them into a committed decision. Context fills with contradictory code and reasoning that never converges.

## Root Cause

Oscillation occurs when the agent has no mechanism to detect it has already visited the current state. Each turn, the model freshly evaluates tradeoffs and reaches a conclusion that contradicts the previous turn — which itself was a correction of the turn before. Without a committed decision record or a "reasoning budget" that forces convergence, the model can alternate indefinitely between two locally-reasonable positions.

## Fix

---

### Option 1: Decision Record — Commit Decisions to Context and Lock Them

After making a significant decision, write it to an explicit decision record that is injected at the start of every subsequent turn, preventing reconsideration.

```python
import anthropic

client = anthropic.Anthropic()

class DecisionRecord:
    def __init__(self):
        self._decisions: dict[str, str] = {}

    def commit(self, topic: str, decision: str, rationale: str) -> None:
        self._decisions[topic] = f"{decision} (Rationale: {rationale})"
        print(f"[DECISION LOCKED] {topic}: {decision}")

    def get_context(self) -> str:
        if not self._decisions:
            return ""
        lines = ["COMMITTED DECISIONS (do NOT revisit or change these):"]
        for topic, decision in self._decisions.items():
            lines.append(f"  • {topic}: {decision}")
        lines.append("\nThese decisions are final. Build on them, don't question them.")
        return "\n".join(lines)

    def has_decision(self, topic: str) -> bool:
        return topic in self._decisions

decisions = DecisionRecord()

def extract_decision(text: str) -> tuple[str, str, str] | None:
    """Parse a decision from agent output. Returns (topic, decision, rationale) or None."""
    import re
    match = re.search(
        r"DECISION:\s*(.+?)\s*=\s*(.+?)\s*BECAUSE:\s*(.+?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
    return None

def run_with_decision_lock(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    for turn in range(10):
        committed_context = decisions.get_context()
        system = (
            "You are a software architect making design decisions.\n\n"
            + (committed_context + "\n\n" if committed_context else "")
            + "When you make an architectural decision, output it as:\n"
            "DECISION: <topic> = <choice> BECAUSE: <one-sentence rationale>\n"
            "Then proceed to implement without reconsidering committed decisions."
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        # Lock any decisions found in the output
        parsed = extract_decision(reply)
        if parsed:
            topic, decision, rationale = parsed
            decisions.commit(topic, decision, rationale)

        # Check for convergence (no more questions or plan changes)
        if "DECISION:" not in reply and "?" not in reply:
            return reply

        messages.append({"role": "user", "content": "Continue implementing based on committed decisions."})

    return "Max turns reached"

result = run_with_decision_lock(
    "Design the backend architecture for a team of 5 engineers building an e-commerce platform"
)
print(result[:300])
```

**Expected Token Savings:** Decision lock eliminates oscillation turns. A 15-turn oscillation loop produces ~15 × 600 = 9,000 tokens of contradictory work. With decision locking: 2-3 turns for the decision + immediate implementation = ~1,800 tokens. Saves 7,200 tokens per oscillating session.
**Environment:** Works for any decision that can be expressed as a key-value pair. Topics should be specific enough to prevent ambiguity (e.g., "database_architecture" not "database"). Store decisions externally for multi-session consistency.

---

### Option 2: Oscillation Detector — Track Plan Reversals and Intervene

Monitor consecutive turns for semantic reversals. When detected, inject a forcing function that demands a final committed answer.

```python
import hashlib
import anthropic

client = anthropic.Anthropic()

def detect_reversal(prev: str, curr: str) -> bool:
    """Heuristic: check for common reversal language."""
    reversal_signals = [
        "actually", "on reflection", "reconsidering", "switching to",
        "reverting", "instead", "changing approach", "better to",
        "on second thought", "let me reconsider",
    ]
    curr_lower = curr.lower()
    has_reversal = any(s in curr_lower for s in reversal_signals)

    # Also check if key terms from prev appear negated in curr
    negation_patterns = ["not ", "don't ", "avoid ", "instead of "]
    words_prev = set(prev.lower().split())
    for negation in negation_patterns:
        for word in words_prev:
            if len(word) > 5 and negation + word in curr_lower:
                return True

    return has_reversal

def run_with_oscillation_detection(task: str, max_reversals: int = 2) -> str:
    messages = [{"role": "user", "content": task}]
    reversal_count = 0
    last_reply = ""
    approach_history: list[str] = []

    for turn in range(12):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="You are an expert technical lead. Make clear, committed decisions.",
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        if last_reply and detect_reversal(last_reply, reply):
            reversal_count += 1
            approach_history.append(reply[:100])
            print(f"[OSCILLATION DETECTED] Reversal #{reversal_count}")

            if reversal_count >= max_reversals:
                # Intervene: force a final decision
                approaches = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(approach_history))
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have changed approach {reversal_count} times. This is your FINAL decision.\n"
                        f"Approaches considered:\n{approaches}\n\n"
                        "Choose ONE approach now and commit to it permanently. "
                        "State your final decision in one sentence, then implement it completely. "
                        "No further reconsideration is permitted."
                    ),
                })
                final_response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    system="You must commit to ONE approach and implement it. No changing course.",
                    messages=messages,
                )
                return final_response.content[0].text

        last_reply = reply

        if "?" not in reply and reversal_count == 0:
            return reply

        messages.append({"role": "user", "content": "Continue."})

    return "Max turns reached"

result = run_with_oscillation_detection(
    "Should I use REST or GraphQL for this API? Design the API layer."
)
print(result[:400])
```

**Expected Token Savings:** Intervention after 2 reversals costs 1 extra turn (~600 tokens) but terminates a loop that would otherwise run 10-15 turns (6,000-9,000 tokens). Net savings: 5,400-8,400 tokens per detected oscillation.
**Environment:** `max_reversals=2` is a good default — allows one genuine course correction, flags anything beyond that as oscillation. Tune reversal signals to your domain vocabulary.

---

### Option 3: Option Evaluation Matrix — Score Tradeoffs Before Committing

Before any plan is executed, require the agent to score all options against explicit criteria. Highest-scoring option wins; no re-evaluation permitted.

```python
import json
import anthropic

client = anthropic.Anthropic()

EVALUATION_PROMPT = """Before implementing, evaluate all viable options using this matrix.

For each option, score 1-5 on each criterion (5=best):
- team_fit: matches team size/skills
- scalability: handles expected growth
- complexity: implementation/operational complexity (5=simple)
- time_to_market: speed to first deploy
- maintenance: long-term maintenance burden (5=low)

Return JSON:
{
  "options": [
    {"name": "Option A", "scores": {"team_fit": 4, "scalability": 3, "complexity": 4, "time_to_market": 5, "maintenance": 4}, "total": 20},
    ...
  ],
  "winner": "Option A",
  "rationale": "one sentence"
}

After returning the JSON, implement the winner without reconsidering."""

def evaluate_and_commit(task: str) -> tuple[dict, str]:
    """Run option evaluation, return (decision_matrix, implementation)."""

    # Step 1: Evaluate options
    eval_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=EVALUATION_PROMPT,
        messages=[{"role": "user", "content": task}],
    )
    raw = eval_response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        matrix = json.loads(raw.strip())
    except json.JSONDecodeError:
        matrix = {"winner": "best_option", "rationale": "could not parse matrix"}

    winner = matrix.get("winner", "unspecified")
    rationale = matrix.get("rationale", "")
    print(f"Decision: {winner} ({rationale})")
    for opt in matrix.get("options", []):
        print(f"  {opt['name']}: total={opt.get('total', '?')}")

    # Step 2: Implement the winner (committed, no second-guessing)
    impl_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            f"Implement '{winner}' exactly as decided. "
            f"Rationale: {rationale}. "
            "Do not reconsider. Do not mention alternative approaches. Just implement."
        ),
        messages=[{"role": "user", "content": task}],
    )
    return matrix, impl_response.content[0].text

matrix, implementation = evaluate_and_commit(
    "For a 5-engineer team building an e-commerce backend that needs to scale to 1M users "
    "in 2 years, choose between: monolith, microservices, or modular monolith."
)
print(f"\nImplementation plan:\n{implementation[:400]}")
```

**Expected Token Savings:** Two-step evaluation adds ~300 tokens but produces a committed decision in 2 turns instead of 15. Savings: 13 oscillation turns × 600 tokens = 7,800 tokens. Net: 7,500 token savings per decision that would have oscillated.
**Environment:** Criteria weights should match your actual priorities. For time-critical projects, weight `time_to_market` higher. Pre-define the criteria set for your domain and reuse it across similar decisions.

---

### Option 4: Plan Fingerprinting — Detect When Agent Revisits Prior State

Hash each plan's key attributes. If the same hash appears twice, the agent is looping — inject the prior reasoning to force forward progress.

```python
import hashlib
import re
import anthropic

client = anthropic.Anthropic()

def plan_fingerprint(text: str) -> str:
    """Extract key structural terms from a plan and hash them."""
    # Extract architecture keywords
    keywords = re.findall(r"\b(microservice|monolith|async|sync|rest|graphql|sql|nosql|"
                          r"redis|postgres|mysql|kafka|queue|cache|serverless|container)\b",
                          text.lower())
    # Normalise: sort + deduplicate
    canonical = "|".join(sorted(set(keywords)))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]

class PlanTracker:
    def __init__(self):
        self._seen: dict[str, tuple[int, str]] = {}  # fingerprint → (turn, summary)

    def check_and_record(self, turn: int, text: str) -> tuple[bool, str | None]:
        """Returns (is_revisit, previous_summary). Records current plan."""
        fp = plan_fingerprint(text)
        if fp in self._seen:
            prev_turn, prev_summary = self._seen[fp]
            return True, f"Turn {prev_turn}: {prev_summary}"
        self._seen[fp] = (turn, text[:100])
        return False, None

tracker = PlanTracker()

def run_with_fingerprinting(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    for turn in range(12):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="You are a software architect. Make clear decisions and implement them.",
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        is_revisit, prev = tracker.check_and_record(turn, reply)

        if is_revisit:
            print(f"[LOOP DETECTED] Turn {turn}: revisiting plan from {prev}")
            messages.append({
                "role": "user",
                "content": (
                    f"You have returned to a plan you already considered ({prev}). "
                    "You are in a loop. You must pick a NEW direction you haven't tried, "
                    "OR commit permanently to your last decision and stop reconsidering. "
                    "Make a final call now."
                ),
            })
            final = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=(
                    "You are making a FINAL, IRREVOCABLE decision. "
                    "State it clearly and implement it. No more deliberation."
                ),
                messages=messages,
            )
            return final.content[0].text

        if "?" not in reply:
            return reply

        messages.append({"role": "user", "content": "Continue."})

    return "Max turns reached"

result = run_with_fingerprinting(
    "Design the data storage layer for a real-time analytics platform"
)
print(result[:400])
```

**Expected Token Savings:** Fingerprinting detects loops in O(1) per turn. Intervention on first detected revisit saves all subsequent oscillation turns. Average oscillation loop: 8 extra turns = 4,800 tokens saved per incident.
**Environment:** Fingerprint granularity is tunable — add/remove keywords from the regex to match your domain. False positives (different plans with same keywords) are acceptable — they result in an extra check, not a wrong decision.

---

### Option 5: Deliberation Budget — Give the Model a Finite Thinking Allowance

Allow N turns of deliberation, then force a conclusion regardless of whether the model feels "ready." Mimics real decision-making under deadline.

```python
import anthropic

client = anthropic.Anthropic()

def run_with_deliberation_budget(task: str, budget: int = 3) -> str:
    messages = [{"role": "user", "content": task}]
    used = 0

    for turn in range(budget + 2):
        remaining = budget - used
        system_note = ""

        if remaining <= 0:
            system_note = (
                "\n\nDELIBERATION BUDGET EXHAUSTED.\n"
                "You must now make a FINAL DECISION and implement it completely.\n"
                "No more 'considering', 'reflecting', or 'on the other hand'.\n"
                "State your choice in one sentence, then implement fully."
            )
        elif remaining == 1:
            system_note = (
                f"\n\nDELIBERATION BUDGET: {remaining} turn remaining.\n"
                "Next response must be your final committed decision + implementation."
            )
        else:
            system_note = f"\n\nDELIBERATION BUDGET: {remaining} turns to deliberate before forced decision."

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="You are a decisive technical lead." + system_note,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        # Count deliberation signals
        deliberation_words = ["considering", "however", "on the other hand", "alternatively",
                              "but", "actually", "reflect", "reconsider"]
        is_deliberating = any(w in reply.lower() for w in deliberation_words)

        if is_deliberating and remaining > 0:
            used += 1
            messages.append({"role": "user", "content": "Continue deliberating if needed, then commit."})
        elif remaining <= 0 or not is_deliberating:
            # Committed
            print(f"Decision made after {used}/{budget} deliberation turns")
            return reply
        else:
            messages.append({"role": "user", "content": "Continue."})

    return "Max deliberation reached — using last response as decision"

result = run_with_deliberation_budget(
    "Should this API use REST or GraphQL? Choose one and design the API layer.",
    budget=2,
)
print(result[:400])
```

**Expected Token Savings:** Budget of 2 deliberation turns caps the oscillation cost at 2 × 600 = 1,200 tokens vs an unbounded loop of 10-15 × 600 = 9,000 tokens. Savings: 7,800 tokens per bounded decision. The forced conclusion on budget exhaustion guarantees termination.
**Environment:** Budget of 2-3 is effective for most architectural decisions. Increase to 4-5 for genuinely ambiguous decisions with large downstream impact. The key is that the budget is visible to the model — it responds to explicit constraints.

---

### Option 6: Multi-Perspective Synthesis — Resolve Oscillation by Integrating Both Views

Instead of choosing between the two options, explicitly ask the model to synthesise them into a single solution that incorporates the best of each.

```python
import anthropic

client = anthropic.Anthropic()

SYNTHESIS_SYSTEM = """You are a senior architect specializing in synthesising conflicting technical viewpoints.

When given two conflicting approaches, your job is NOT to pick one — it's to find the synthesis:
- What does each approach correctly identify as important?
- What does each approach get wrong or overweight?
- What design resolves both concerns simultaneously?

Structure your response:
1. Core insight from Approach A: [what's right about it]
2. Core insight from Approach B: [what's right about it]
3. Synthesised solution: [the design that honours both insights]
4. Implementation: [concrete implementation of the synthesised solution]

Never say "it depends" without immediately specifying what it depends on and resolving it."""

def synthesise_conflicting_plans(task: str, plan_a: str, plan_b: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYNTHESIS_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Task: {task}\n\n"
                f"Approach A (microservices rationale): {plan_a}\n\n"
                f"Approach B (monolith rationale): {plan_b}\n\n"
                "Synthesise these into a single concrete solution."
            ),
        }],
    )
    return response.content[0].text

def run_with_synthesis_fallback(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    plan_history: list[str] = []

    for turn in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="You are a software architect. Make a decision and implement it.",
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})
        plan_history.append(reply)

        # Detect oscillation after 2+ plans
        if len(plan_history) >= 2 and any(
            sig in reply.lower()
            for sig in ["actually", "on reflection", "switching", "reverting", "reconsidering"]
        ):
            print(f"[SYNTHESIS MODE] Oscillation detected at turn {turn}. Synthesising.")
            return synthesise_conflicting_plans(task, plan_history[-2], plan_history[-1])

        if "?" not in reply:
            return reply

        messages.append({"role": "user", "content": "Continue."})

    return "Max turns"

# Comparison table
"""
| Approach | Termination Guarantee | Tokens Saved | Decision Quality | Complexity |
|---|---|---|---|---|
| Option 1: Decision lock | Strong | High | Good | Low |
| Option 2: Reversal detector | Strong | High | Good | Low |
| Option 3: Evaluation matrix | Strong | Very High | Very Good | Medium |
| Option 4: Plan fingerprinting | Strong | High | Good | Low |
| Option 5: Deliberation budget | Absolute | Very High | Good | Low |
| Option 6: Synthesis | Strong | High | Best | Medium |
"""

result = run_with_synthesis_fallback(
    "Design the data access layer — should we use an ORM or raw SQL queries?"
)
print(result[:500])
```

**Expected Token Savings:** Synthesis produces a better answer than either option alone, which prevents future re-opening of the debate. Terminates in 3-4 turns (2 initial proposals + 1 synthesis) vs 10-15 oscillation turns. Saves ~6,600-7,800 tokens while producing higher-quality output.
**Environment:** Synthesis works best when both options have genuine merit (neither is clearly wrong). If one option is clearly better, use Option 1 (decision lock) or Option 5 (budget) instead. Synthesis adds ~200 tokens to the system prompt but the quality improvement is typically worth it.
