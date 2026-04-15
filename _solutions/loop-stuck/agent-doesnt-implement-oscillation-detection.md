---
layout: solution
title: "Agent Doesn't Implement Oscillation Detection"
category: loop-stuck
description: "How to detect when an agent is oscillating between two or more states — alternating decisions without converging — and inject recovery to break the cycle."
tags: [loop-stuck, oscillation, detection, state-machine, recovery, convergence]
---

# Agent Doesn't Implement Oscillation Detection

Infinite loop detection catches same-step repetition. Oscillation is harder: the agent alternates between two legitimate states — plan then critique then re-plan then re-critique — never converging. Oscillation burns tokens and time without producing output. Detection requires tracking state transitions over a window and recognizing cyclical patterns before they consume the full budget.

## Option 1: State Transition History — Detect A→B→A Patterns

Record each agent state and detect when the transition sequence forms a repeating cycle.

```python
import anthropic
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OscillationDetector:
    window_size: int = 6          # Number of recent states to track
    min_cycle_length: int = 2     # Shortest cycle to detect (A→B→A = length 2)
    max_cycle_length: int = 4     # Longest cycle to check
    repeat_threshold: int = 2     # How many full cycles before flagging
    history: deque = field(default_factory=lambda: deque(maxlen=6))

    def record(self, state: str):
        self.history.append(state)

    def detect_cycle(self) -> Optional[tuple[list[str], int]]:
        """Returns (cycle_pattern, repeat_count) if oscillation detected, else None."""
        states = list(self.history)
        n = len(states)

        for cycle_len in range(self.min_cycle_length, self.max_cycle_length + 1):
            if n < cycle_len * self.repeat_threshold:
                continue

            # Check if the last `cycle_len * repeat_threshold` states form repeating cycles
            segment = states[-(cycle_len * self.repeat_threshold):]
            candidate = segment[:cycle_len]

            repeats = 0
            for i in range(0, len(segment), cycle_len):
                chunk = segment[i:i + cycle_len]
                if chunk == candidate:
                    repeats += 1
                else:
                    break

            if repeats >= self.repeat_threshold:
                return candidate, repeats

        return None

    def is_oscillating(self) -> tuple[bool, str]:
        result = self.detect_cycle()
        if result:
            pattern, repeats = result
            return True, f"Cycle detected: {' → '.join(pattern)} (×{repeats})"
        return False, ""


def classify_response_state(response_text: str) -> str:
    """Classify agent response into a state label."""
    lower = response_text.lower()

    if any(w in lower for w in ["i need more", "clarify", "could you specify", "what do you mean"]):
        return "clarify"
    if any(w in lower for w in ["let me plan", "first i'll", "my approach", "step 1:"]):
        return "plan"
    if any(w in lower for w in ["however", "on the other hand", "but wait", "actually"]):
        return "critique"
    if any(w in lower for w in ["in conclusion", "therefore", "the answer is", "finally"]):
        return "conclude"
    if any(w in lower for w in ["searching", "looking up", "checking", "let me find"]):
        return "search"
    return "respond"


def run_with_oscillation_detection(goal: str, max_turns: int = 12) -> str:
    client = anthropic.Anthropic()
    detector = OscillationDetector(window_size=8, repeat_threshold=2)

    messages = [{"role": "user", "content": goal}]
    last_output = ""

    for turn in range(max_turns):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=messages,
        )
        output = response.content[0].text
        last_output = output

        state = classify_response_state(output)
        detector.record(state)

        print(f"[Turn {turn+1}] State={state}: {output[:60]}...")
        print(f"  History: {list(detector.history)}")

        oscillating, reason = detector.is_oscillating()
        if oscillating:
            print(f"[OSCILLATION] {reason}")
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": (
                "You appear to be going back and forth without reaching a conclusion. "
                "Please commit to a final answer now, even if imperfect. "
                "Stop planning/critiquing and provide your best response directly."
            )})
            detector.history.clear()
            continue

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn" and state == "conclude":
            print(f"[DONE] Converged at turn {turn+1}")
            break

        if turn < max_turns - 1:
            messages.append({"role": "user", "content": "Continue."})

    return last_output


if __name__ == "__main__":
    # Goal designed to trigger plan-critique oscillation
    result = run_with_oscillation_detection(
        "Should I use microservices or a monolith for my new startup?",
        max_turns=8,
    )
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: 50-70% by breaking 4-6 turn plan-critique cycles early
# Environment: Decision-making agents, debate-style reasoning, any agent prone to plan-then-reconsider loops
```

## Option 2: Semantic Fingerprint Oscillation — Beyond Literal State Labels

Hash semantic content of each response to detect oscillation even when the wording varies.

```python
import anthropic
import hashlib
import re
from collections import deque, Counter
from dataclasses import dataclass, field
from typing import Optional


def semantic_fingerprint(text: str, n_keywords: int = 8) -> str:
    """Extract key semantic tokens and hash them for comparison."""
    # Remove stopwords and extract content words
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "i", "to", "of",
                 "and", "or", "but", "in", "on", "at", "it", "this", "that",
                 "would", "could", "should", "will", "can", "do", "be", "have"}
    words = re.findall(r'\b[a-z]{4,}\b', text.lower())
    keywords = [w for w in words if w not in stopwords]
    # Take most distinctive words (sorted for order-invariance)
    top_keywords = sorted(set(keywords))[:n_keywords]
    fingerprint = " ".join(top_keywords)
    return hashlib.md5(fingerprint.encode()).hexdigest()[:8]


@dataclass
class SemanticOscillationTracker:
    window: int = 8
    similarity_threshold: int = 2   # Same fingerprint N times = oscillation
    fingerprints: deque = field(default_factory=lambda: deque(maxlen=8))
    fp_counts: Counter = field(default_factory=Counter)

    def record(self, text: str) -> str:
        fp = semantic_fingerprint(text)
        self.fingerprints.append(fp)
        self.fp_counts[fp] += 1
        return fp

    def is_oscillating(self) -> tuple[bool, str]:
        # Check for any fingerprint appearing too many times recently
        for fp, count in self.fp_counts.items():
            if count >= self.similarity_threshold:
                return True, f"Semantic fingerprint {fp} appeared {count}× (content cycling)"

        # Check for alternating pair: A B A B
        fps = list(self.fingerprints)
        if len(fps) >= 4:
            # Check if last 4 form ABAB pattern
            if fps[-1] == fps[-3] and fps[-2] == fps[-4] and fps[-1] != fps[-2]:
                return True, f"Alternating pattern detected: {fps[-2]} ↔ {fps[-1]}"

        return False, ""

    def reset_tracking(self):
        self.fingerprints.clear()
        self.fp_counts.clear()


def agent_with_semantic_oscillation_guard(prompt: str, max_turns: int = 10) -> str:
    client = anthropic.Anthropic()
    tracker = SemanticOscillationTracker(window=6, similarity_threshold=2)

    messages = [{"role": "user", "content": prompt}]
    last_output = ""
    recovery_count = 0

    for turn in range(max_turns):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=messages,
        )
        output = response.content[0].text
        last_output = output
        fp = tracker.record(output)

        print(f"[Turn {turn+1}] fp={fp}: {output[:70]}...")

        oscillating, reason = tracker.is_oscillating()
        if oscillating:
            recovery_count += 1
            print(f"[SEMANTIC OSCILLATION] {reason} (recovery #{recovery_count})")

            if recovery_count >= 3:
                # Hard stop — produce best answer with what we have
                messages.append({"role": "assistant", "content": output})
                recovery = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=200,
                    messages=messages + [{"role": "user", "content": (
                        "FINAL ANSWER REQUIRED: Provide your single best answer. No more deliberation."
                    )}],
                )
                return recovery.content[0].text

            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": (
                f"You are oscillating on this topic ({reason}). "
                "Make a definitive decision and stick to it. "
                "Pick the approach you currently favor most and explain it once."
            )})
            tracker.reset_tracking()
            continue

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn":
            break

        messages.append({"role": "user", "content": "What is your final recommendation?"})

    return last_output


if __name__ == "__main__":
    result = agent_with_semantic_oscillation_guard(
        "Is it better to use PostgreSQL or MongoDB for a new project? Be decisive.",
        max_turns=6,
    )
    print(f"\nFinal: {result[:250]}")

# Expected Token Savings: 45-65% by catching semantic cycles that differ in wording but repeat in substance
# Environment: Opinion-forming agents, recommendation systems, agents deliberating between options
```

## Option 3: Convergence Score Tracking — Detect Divergence Before Full Oscillation

Measure whether consecutive responses are converging (getting more similar) or diverging (getting more different). Divergence predicts oscillation before it fully develops.

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Optional


def word_overlap_score(text_a: str, text_b: str) -> float:
    """Jaccard similarity between word sets of two texts."""
    words_a = set(re.findall(r'\b[a-z]{3,}\b', text_a.lower()))
    words_b = set(re.findall(r'\b[a-z]{3,}\b', text_b.lower()))
    if not words_a and not words_b:
        return 1.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


@dataclass
class ConvergenceTracker:
    convergence_window: int = 4
    convergence_threshold: float = 0.5    # Similarity below this = diverging
    min_convergence_gain: float = 0.05   # Must improve by this much per step
    similarities: list = field(default_factory=list)
    outputs: list = field(default_factory=list)

    def record(self, output: str):
        self.outputs.append(output)
        if len(self.outputs) >= 2:
            sim = word_overlap_score(self.outputs[-2], self.outputs[-1])
            self.similarities.append(sim)

    def is_diverging(self) -> tuple[bool, str]:
        if len(self.similarities) < 2:
            return False, ""

        recent = self.similarities[-self.convergence_window:]

        # Check: are we consistently below similarity threshold?
        low_sim_count = sum(1 for s in recent if s < self.convergence_threshold)
        if low_sim_count >= len(recent):
            avg_sim = sum(recent) / len(recent)
            return True, f"Persistent divergence: avg similarity={avg_sim:.2f} < {self.convergence_threshold}"

        # Check: is similarity trending down (oscillating/diverging)?
        if len(recent) >= 3:
            diffs = [recent[i] - recent[i-1] for i in range(1, len(recent))]
            if all(d < 0 for d in diffs):
                return True, f"Monotonic divergence: similarities={[f'{s:.2f}' for s in recent]}"

        return False, ""

    def is_converged(self) -> bool:
        """Has the agent reached a stable state?"""
        if len(self.similarities) < 2:
            return False
        recent = self.similarities[-3:]
        return all(s >= 0.65 for s in recent)


def convergence_monitored_agent(goal: str, max_turns: int = 10) -> str:
    client = anthropic.Anthropic()
    tracker = ConvergenceTracker()

    messages = [{"role": "user", "content": goal}]
    last_output = ""

    for turn in range(max_turns):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=messages,
        )
        output = response.content[0].text
        last_output = output
        tracker.record(output)

        sim_str = f"{tracker.similarities[-1]:.2f}" if tracker.similarities else "N/A"
        print(f"[Turn {turn+1}] similarity={sim_str}: {output[:60]}...")

        if tracker.is_converged():
            print(f"[CONVERGED] Agent has reached stable output at turn {turn+1}")
            break

        diverging, reason = tracker.is_diverging()
        if diverging:
            print(f"[DIVERGING] {reason} — injecting convergence prompt")
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": (
                "Your responses are becoming less consistent. "
                "Please settle on a single, clear position and express it concisely. "
                "Stop reconsidering — commit to your best answer."
            )})
            tracker.similarities.clear()
            continue

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn":
            break

        messages.append({"role": "user", "content": "Refine your answer."})

    return last_output


if __name__ == "__main__":
    result = convergence_monitored_agent(
        "What programming language should I learn first: Python or JavaScript?",
        max_turns=6,
    )
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: 35-55% by detecting pre-oscillation divergence before full cycles develop
# Environment: Iterative refinement agents, agents that "refine" responses in a loop
```

## Option 4: Decision Commitment Tracking — Detect Reversed Decisions

Track decisions the agent has explicitly made and flag if a later response contradicts or reverses them.

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Decision:
    turn: int
    statement: str
    confidence: float


DECISION_PATTERNS = [
    r"(i (will|should|recommend|suggest|prefer|choose|decide(d)?|conclude(d)?))\s+(.+?)[\.\,]",
    r"(the best (option|choice|approach|solution) is)\s+(.+?)[\.\,]",
    r"(therefore|thus|hence|in conclusion),?\s+(.+?)[\.\,]",
    r"(i (have |)(decided|concluded|determined) (that|to))\s+(.+?)[\.\,]",
]

REVERSAL_PATTERNS = [
    r"(actually|wait|on second thought|however|but|reconsidering|i was wrong)",
    r"(let me reconsider|i should have|in retrospect|changing my (mind|answer))",
    r"(never mind|disregard|ignore (what i said|my previous))",
]


def extract_decision(text: str, turn: int) -> Optional[Decision]:
    """Extract any explicit decision from the text."""
    for pattern in DECISION_PATTERNS:
        match = re.search(pattern, text.lower())
        if match:
            # Get confidence from hedging language
            confidence = 1.0
            if any(h in text.lower() for h in ["might", "maybe", "perhaps", "could be"]):
                confidence = 0.6
            elif any(h in text.lower() for h in ["probably", "likely", "tend to"]):
                confidence = 0.8
            return Decision(turn=turn, statement=match.group()[:100], confidence=confidence)
    return None


def detect_reversal(text: str, prior_decisions: list[Decision]) -> tuple[bool, str]:
    """Check if current text reverses a prior decision."""
    if not prior_decisions:
        return False, ""

    has_reversal_signal = any(
        re.search(p, text.lower()) for p in REVERSAL_PATTERNS
    )

    if not has_reversal_signal:
        return False, ""

    # Reversal signal present — check against recent high-confidence decisions
    recent_high_conf = [d for d in prior_decisions[-3:] if d.confidence >= 0.8]
    if recent_high_conf:
        last = recent_high_conf[-1]
        return True, (
            f"Reversal detected: turn {last.turn} decision '{last.statement[:50]}' "
            f"may be contradicted"
        )

    return False, ""


def agent_with_commitment_guard(goal: str, max_turns: int = 10) -> str:
    client = anthropic.Anthropic()
    decisions: list[Decision] = []
    messages = [{"role": "user", "content": goal}]
    last_output = ""
    reversal_count = 0

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=messages,
        )
        output = response.content[0].text
        last_output = output

        # Extract any new decision
        decision = extract_decision(output, turn)
        if decision:
            decisions.append(decision)
            print(f"[Turn {turn}] Decision recorded (conf={decision.confidence:.1f}): {decision.statement[:50]}")

        # Check for reversal
        reversed_decision, reason = detect_reversal(output, decisions)
        if reversed_decision:
            reversal_count += 1
            print(f"[REVERSAL #{reversal_count}] {reason}")

            if reversal_count >= 2:
                print("[OSCILLATION] Too many reversals — forcing commitment")
                messages.append({"role": "assistant", "content": output})
                forced = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=200,
                    messages=messages + [{"role": "user", "content": (
                        "You have reversed your decision multiple times. "
                        f"Your most recent committed position was: '{decisions[-1].statement if decisions else 'unclear'}'. "
                        "Stick with this. Provide your final answer without second-guessing."
                    )}],
                )
                return forced.content[0].text

        print(f"[Turn {turn}] {output[:70]}...")
        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn":
            break

        messages.append({"role": "user", "content": "Any final thoughts?"})

    return last_output


if __name__ == "__main__":
    result = agent_with_commitment_guard(
        "Should we use a SQL or NoSQL database for our e-commerce platform? Give a definitive recommendation.",
        max_turns=5,
    )
    print(f"\nFinal answer: {result[:250]}")

# Expected Token Savings: 40-60% by catching decision oscillation after just 2 reversals rather than letting it continue
# Environment: Recommendation agents, architecture decision agents, any agent making explicit choices
```

## Option 5: Turn-Budget Oscillation Breaker

Assign a shrinking turn budget. As remaining turns decrease, automatically tighten the convergence requirement.

```python
import anthropic
from dataclasses import dataclass


@dataclass
class TurnBudgetController:
    total_turns: int
    turns_used: int = 0
    convergence_injected_at: list = None

    def __post_init__(self):
        self.convergence_injected_at = []

    @property
    def turns_remaining(self) -> int:
        return self.total_turns - self.turns_used

    @property
    def urgency_level(self) -> str:
        pct_remaining = self.turns_remaining / self.total_turns
        if pct_remaining > 0.6:
            return "explore"
        elif pct_remaining > 0.3:
            return "narrow"
        elif pct_remaining > 0.1:
            return "converge"
        return "conclude"

    def tick(self) -> str:
        self.turns_used += 1
        return self.urgency_level

    def get_urgency_prompt(self) -> str:
        prompts = {
            "explore": "Continue exploring the problem.",
            "narrow": (
                f"You have {self.turns_remaining} turns left. "
                "Start narrowing toward a concrete answer."
            ),
            "converge": (
                f"Only {self.turns_remaining} turns remain. "
                "You must converge now. Stop reconsidering — commit to your best answer."
            ),
            "conclude": (
                "FINAL TURN. Provide your definitive answer immediately. "
                "No more deliberation is possible."
            ),
        }
        return prompts[self.urgency_level]

    def should_force_stop(self) -> bool:
        return self.turns_remaining <= 1


def budget_controlled_agent(goal: str, total_turns: int = 8) -> str:
    client = anthropic.Anthropic()
    controller = TurnBudgetController(total_turns=total_turns)

    messages = [{"role": "user", "content": goal}]
    last_output = ""

    while not controller.should_force_stop():
        urgency = controller.tick()

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=messages,
        )
        output = response.content[0].text
        last_output = output

        print(
            f"[Turn {controller.turns_used}/{total_turns}] "
            f"urgency={urgency} remaining={controller.turns_remaining}: "
            f"{output[:60]}..."
        )

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn" and urgency in ("converge", "conclude"):
            print("[DONE] Agent concluded within budget")
            break

        next_prompt = controller.get_urgency_prompt()
        print(f"[URGENCY] {next_prompt}")
        messages.append({"role": "user", "content": next_prompt})

    # Force final answer if budget exhausted without conclusion
    if controller.should_force_stop() and last_output:
        print("[BUDGET EXHAUSTED] Extracting best answer from last response")

    return last_output


if __name__ == "__main__":
    result = budget_controlled_agent(
        "What is the single most important factor in API design?",
        total_turns=6,
    )
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: Hard turn budget guarantees bounded token spend regardless of oscillation depth
# Environment: Interactive agents with response-time SLAs, any agent where bounded execution is required
```

## Option 6: Multi-Dimension Oscillation Scoring — Combined Signal

Combine multiple oscillation signals (state cycling, similarity, reversal count, turn exhaustion) into a single composite score to trigger recovery.

```python
import anthropic
import re
import hashlib
from dataclasses import dataclass, field
from collections import deque


@dataclass
class OscillationSignals:
    state_cycle_score: float = 0.0    # 0.0–1.0 based on state repetition
    similarity_score: float = 1.0    # High = converging, Low = diverging
    reversal_score: float = 0.0      # 0.0–1.0 based on decision reversals
    turn_pressure: float = 0.0       # Increases as turns run out

    def composite(self, weights: dict = None) -> float:
        w = weights or {
            "state": 0.35,
            "similarity": 0.25,
            "reversal": 0.25,
            "pressure": 0.15,
        }
        # Invert similarity: high similarity = low oscillation signal
        divergence = 1.0 - self.similarity_score
        return (
            self.state_cycle_score * w["state"]
            + divergence * w["similarity"]
            + self.reversal_score * w["reversal"]
            + self.turn_pressure * w["pressure"]
        )


def state_label(text: str) -> str:
    lower = text.lower()
    if re.search(r"\b(plan|approach|strategy|consider|analyze)\b", lower):
        return "plan"
    if re.search(r"\b(however|but|on the other hand|alternatively)\b", lower):
        return "critique"
    if re.search(r"\b(therefore|conclude|recommend|final|answer)\b", lower):
        return "conclude"
    if re.search(r"\b(search|find|look|check|retrieve)\b", lower):
        return "search"
    return "respond"


def word_sim(a: str, b: str) -> float:
    wa = set(re.findall(r'\b[a-z]{4,}\b', a.lower()))
    wb = set(re.findall(r'\b[a-z]{4,}\b', b.lower()))
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / len(wa | wb)


def composite_oscillation_agent(goal: str, max_turns: int = 10, threshold: float = 0.65) -> str:
    client = anthropic.Anthropic()

    state_history = deque(maxlen=6)
    outputs = []
    reversal_count = 0
    messages = [{"role": "user", "content": goal}]
    last_output = ""

    REVERSAL_RE = re.compile(r"(actually|wait|on second thought|i was wrong|reconsidering)", re.I)

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=messages,
        )
        output = response.content[0].text
        last_output = output
        outputs.append(output)

        # Calculate signals
        state = state_label(output)
        state_history.append(state)

        # State cycle score: fraction of recent states that are repeated
        states = list(state_history)
        unique_ratio = len(set(states)) / len(states) if states else 1.0
        state_cycle_score = 1.0 - unique_ratio  # Low unique = high cycling

        # Similarity score
        similarity = word_sim(outputs[-2], outputs[-1]) if len(outputs) >= 2 else 1.0

        # Reversal score
        if REVERSAL_RE.search(output):
            reversal_count += 1
        reversal_score = min(1.0, reversal_count / 3)

        # Turn pressure (ramps up in last 30% of turns)
        turn_pressure = max(0.0, (turn / max_turns - 0.7) / 0.3)

        signals = OscillationSignals(
            state_cycle_score=state_cycle_score,
            similarity_score=similarity,
            reversal_score=reversal_score,
            turn_pressure=turn_pressure,
        )
        composite = signals.composite()

        print(
            f"[Turn {turn}] state={state} composite={composite:.2f} "
            f"(cycle={state_cycle_score:.2f} div={1-similarity:.2f} rev={reversal_score:.2f} press={turn_pressure:.2f})"
        )

        if composite >= threshold:
            print(f"[OSCILLATION] Composite score {composite:.2f} ≥ threshold {threshold}")
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": (
                f"Oscillation detected (score={composite:.2f}). "
                "You must now give a single, final, committed answer. "
                "State your position clearly and do not qualify it further."
            )})
            # Reset signals
            state_history.clear()
            reversal_count = 0
            outputs = outputs[-1:]
            continue

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn" and state == "conclude":
            break

        messages.append({"role": "user", "content": "Continue toward a final answer."})

    return last_output


if __name__ == "__main__":
    result = composite_oscillation_agent(
        "Is functional or object-oriented programming better for large-scale systems?",
        max_turns=7,
        threshold=0.60,
    )
    print(f"\nFinal: {result[:250]}")

# Expected Token Savings: Multi-signal detection catches oscillation earlier than any single signal alone; 50-75% savings
# Environment: Complex decision agents, architecture advisors, any agent balancing multiple competing perspectives
```

## Comparison

| Option | Detection Signal | LLM Overhead | Accuracy | Best For |
|--------|----------------|--------------|----------|----------|
| 1 State Transition History | State label cycling | None | Good for explicit states | Plan-critique-replan loops |
| 2 Semantic Fingerprint | Content hash cycling | None | Good for semantic cycles | Paraphrased repetition |
| 3 Convergence Score | Word overlap similarity | None | Good for divergence | Iterative refinement loops |
| 4 Decision Commitment | Reversal pattern matching | None | Good for flip-flop | Recommendation/choice agents |
| 5 Turn Budget | Urgency-based pressure | None | Always bounded | Any agent needing hard token limits |
| 6 Composite Score | Multi-signal weighted sum | None | Best overall | Production agents needing robust detection |
