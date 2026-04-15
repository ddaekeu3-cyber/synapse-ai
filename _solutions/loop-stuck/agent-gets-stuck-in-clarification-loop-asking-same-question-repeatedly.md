---
layout: solution
title: "Agent Gets Stuck in Clarification Loop Asking Same Question Repeatedly"
category: loop-stuck
description: "Agent asks a clarifying question, receives an answer, then asks the same question again — looping indefinitely without making progress."
tags: [loop-stuck, clarification, conversation, state-tracking, infinite-loop]
---

## Symptom

Agent repeats the same clarifying question despite receiving answers:

```
Agent: "What programming language should I use for this project?"
User: "Python"
Agent: "I understand. What programming language should I use for this project?"
User: "I said Python!"
Agent: "Got it. To make sure I understand — what programming language would you prefer?"
User: [gives up]

# Or more subtle:
Agent: "Could you clarify what you mean by 'fast'?"
User: "I need responses under 200ms"
Agent: "I see. When you say fast, could you be more specific?"
User: "200 MILLISECONDS. I said this."
Agent: "Understood. Just to confirm — what performance target are you aiming for?"
```

The agent semantically fails to recognise that its question was already answered, either because it rephrases too broadly, doesn't track what was asked vs answered, or re-reads the conversation and confuses the question pattern.

## Root Cause

Clarification loops emerge from two failure modes: (1) the model's system prompt or instructions encourage asking clarifying questions without a mechanism to track which questions have already been answered, and (2) the model processes context globally and loses track of the specific Q&A pair when the conversation grows long. Each turn the model sees "there is ambiguity about X" and asks again, ignoring the earlier answer.

## Fix

---

### Option 1: Explicit Clarification Tracker — Inject Answered Questions into Context

Maintain a dict of asked questions and their answers. Inject the answered Q&A pairs into the system prompt so the model knows not to re-ask.

```python
import anthropic

client = anthropic.Anthropic()

class ClarificationTracker:
    def __init__(self):
        self.asked: dict[str, str | None] = {}  # question → answer (None = pending)

    def mark_asked(self, question: str) -> None:
        if question not in self.asked:
            self.asked[question] = None

    def mark_answered(self, question: str, answer: str) -> None:
        self.asked[question] = answer

    def get_answered(self) -> dict[str, str]:
        return {q: a for q, a in self.asked.items() if a is not None}

    def get_pending(self) -> list[str]:
        return [q for q, a in self.asked.items() if a is None]

    def build_context(self) -> str:
        answered = self.get_answered()
        if not answered:
            return ""
        lines = ["ALREADY ANSWERED CLARIFICATIONS (do NOT ask again):"]
        for q, a in answered.items():
            lines.append(f"  Q: {q}")
            lines.append(f"  A: {a}")
        return "\n".join(lines)

tracker = ClarificationTracker()

def extract_clarification_question(text: str) -> str | None:
    """Detect if the assistant asked a clarifying question."""
    question_signals = ["?", "could you clarify", "what do you mean", "can you specify"]
    if any(s in text.lower() for s in question_signals) and "?" in text:
        # Extract the last sentence ending with ?
        sentences = text.split(".")
        for s in reversed(sentences):
            if "?" in s:
                return s.strip()
    return None

def run_session(user_task: str) -> None:
    messages = [{"role": "user", "content": user_task}]

    # Simulate user answers
    simulated_answers = {
        "What programming language": "Python",
        "What performance": "under 200ms latency",
        "target environment": "AWS Lambda",
    }

    for _ in range(8):
        # Inject clarification context into system prompt
        answered_context = tracker.build_context()
        system = (
            "You are a helpful software architect. Ask ONE clarifying question at a time if needed.\n\n"
            + answered_context
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        # Detect if agent asked a question
        q = extract_clarification_question(reply)
        if q:
            tracker.mark_asked(q)
            # Find matching simulated answer
            answer = next(
                (v for k, v in simulated_answers.items() if k.lower() in q.lower()),
                "I'm not sure, use your best judgment",
            )
            print(f"Agent asked: {q!r}")
            print(f"User answers: {answer!r}\n")
            tracker.mark_answered(q, answer)
            messages.append({"role": "user", "content": answer})
        else:
            print(f"Agent proceeding: {reply[:150]}...")
            break

run_session("Build me a web scraper")
```

**Expected Token Savings:** Answered context injection (~80 tokens) prevents the loop from running 3-5 extra turns × 400 tokens each = 1,200-2,000 tokens wasted. Net savings: 1,100-1,900 tokens per session with clarification loops.
**Environment:** Works for single-session conversations. For multi-session, persist the tracker to a database between sessions.

---

### Option 2: Strict One-Question Policy with Progress Gate

Enforce a policy that the agent may ask at most one pending question per turn, and must make concrete progress between clarification questions.

```python
import anthropic
import re

client = anthropic.Anthropic()

ONE_QUESTION_POLICY = """
## Clarification Policy

1. Ask at most ONE question per response
2. Never ask a question that was already answered in this conversation
3. After receiving any answer (even vague), make concrete progress before asking another question
4. If the user's answer is incomplete, make a reasonable assumption and state it explicitly
5. Do NOT rephrase or re-ask the same question in different words

When you receive an answer:
- Acknowledge it explicitly: "Got it, using Python."
- Proceed with the task using that answer
- Only ask another question if genuinely blocked (not just unsure)

If you realise you already asked this question: stop, search the conversation, find the answer, use it.
"""

def count_questions_in_text(text: str) -> int:
    return text.count("?")

def run_with_policy(initial_task: str, user_responses: list[str]) -> str:
    messages = [{"role": "user", "content": initial_task}]
    response_iter = iter(user_responses)
    questions_asked: list[str] = []

    for turn in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=ONE_QUESTION_POLICY,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        q_count = count_questions_in_text(reply)
        print(f"Turn {turn+1}: {q_count} question(s). Reply: {reply[:100]}...")

        if q_count > 1:
            print(f"WARNING: Agent asked {q_count} questions in one turn — policy violation")

        if q_count == 0:
            # No question — task is progressing
            return reply

        # Check for repeated question
        new_questions = re.findall(r"[^.!?]*\?", reply)
        for q in new_questions:
            q_clean = q.strip().lower()
            similar = [prev for prev in questions_asked
                      if len(set(q_clean.split()) & set(prev.split())) > 3]
            if similar:
                print(f"LOOP DETECTED: Agent re-asked '{q_clean}' — similar to '{similar[0]}'")

        questions_asked.extend(q.strip().lower() for q in new_questions)

        # Provide next user response
        try:
            user_answer = next(response_iter)
        except StopIteration:
            user_answer = "Please use your best judgment."

        messages.append({"role": "user", "content": user_answer})

    return "Max turns reached"

result = run_with_policy(
    "Create a data processing pipeline",
    ["Python", "We process about 10GB of CSV files daily", "Output to PostgreSQL"],
)
print(f"\nFinal: {result[:200]}")
```

**Expected Token Savings:** Policy enforcement terminates loops after 1-2 clarifications instead of 5-10. For a 6-turn loop saved: 4 turns × 400 tokens = 1,600 tokens. System prompt adds ~250 tokens once.
**Environment:** The policy works best when included in the base system prompt from the start. Retroactively adding it after a loop starts is less effective.

---

### Option 3: Conversation State Machine — Track Clarification Phase Explicitly

Model the conversation as a state machine with phases (gathering-info, working, done). Force state transitions after each clarification is answered.

```python
import anthropic
from enum import Enum, auto

class Phase(Enum):
    GATHERING = auto()    # Collecting requirements
    WORKING = auto()      # Executing the task
    REVIEWING = auto()    # Presenting results
    DONE = auto()

client = anthropic.Anthropic()

class StatefulAgent:
    def __init__(self, task: str):
        self.task = task
        self.phase = Phase.GATHERING
        self.gathered: dict[str, str] = {}
        self.messages: list[dict] = [{"role": "user", "content": task}]
        self.questions_asked: set[str] = set()

    def _system_for_phase(self) -> str:
        gathered_str = "\n".join(f"  {k}: {v}" for k, v in self.gathered.items())
        common = f"Task: {self.task}\n\nGathered information:\n{gathered_str or '  (none yet)'}"

        if self.phase == Phase.GATHERING:
            already_asked = "\n".join(f"  - {q}" for q in self.questions_asked) or "  (none)"
            return (
                f"{common}\n\nPhase: GATHERING\n"
                f"Already asked (do NOT re-ask):\n{already_asked}\n\n"
                "Ask the SINGLE most important missing question, or if you have enough info, "
                "reply with EXACTLY: READY_TO_PROCEED"
            )
        elif self.phase == Phase.WORKING:
            return (
                f"{common}\n\nPhase: WORKING\n"
                "Implement the task using the gathered information. Do not ask more questions."
            )
        return f"{common}\n\nPhase: {self.phase.name}"

    def _detect_new_info(self, user_msg: str, last_question: str | None) -> None:
        """Store user answer as gathered info."""
        if last_question and user_msg.strip():
            key = last_question[:40].rstrip("?").strip()
            self.gathered[key] = user_msg[:200]

    def run(self, user_responses: list[str]) -> str:
        response_iter = iter(user_responses)
        last_question = None

        for turn in range(12):
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=self._system_for_phase(),
                messages=self.messages,
            )
            reply = response.content[0].text.strip()
            self.messages.append({"role": "assistant", "content": reply})

            if reply == "READY_TO_PROCEED":
                print(f"[Turn {turn+1}] Phase transition: GATHERING → WORKING")
                self.phase = Phase.WORKING
                self.messages.append({"role": "user", "content": "Please proceed."})
                continue

            if self.phase == Phase.GATHERING:
                if "?" in reply:
                    last_question = reply
                    self.questions_asked.add(reply[:80])
                    try:
                        user_answer = next(response_iter)
                    except StopIteration:
                        user_answer = "Use your judgment."
                    print(f"[Turn {turn+1}] Q: {reply[:80]!r} → A: {user_answer!r}")
                    self._detect_new_info(user_answer, last_question)
                    self.messages.append({"role": "user", "content": user_answer})
                else:
                    print(f"[Turn {turn+1}] GATHERING but no question — forcing READY_TO_PROCEED")
                    self.phase = Phase.WORKING
            elif self.phase == Phase.WORKING:
                print(f"[Turn {turn+1}] WORKING: {reply[:100]}...")
                return reply

        return "Max turns"

agent = StatefulAgent("Build a REST API for user authentication")
result = agent.run(["Python/FastAPI", "JWT tokens", "PostgreSQL for user storage"])
print(f"\nResult: {result[:200]}")
```

**Expected Token Savings:** State machine prevents phase regression — once in WORKING phase, the agent cannot return to GATHERING and re-ask questions. Saves all turns from a full clarification loop restart. For a 4-question loop caught: ~2,000 tokens.
**Environment:** State machine adds complexity but is essential for long-running agents with distinct phases. Persist phase and gathered state to a database for multi-session continuity.

---

### Option 4: Semantic Deduplication — Detect Rephrased Duplicate Questions

Use embedding similarity to detect when the agent is asking a question that is semantically equivalent to one already asked.

```python
import math
import anthropic

client = anthropic.Anthropic()

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x ** 2 for x in a))
    mag_b = math.sqrt(sum(x ** 2 for x in b))
    return dot / (mag_a * mag_b + 1e-8)

class SemanticQuestionDeduplicator:
    def __init__(self, similarity_threshold: float = 0.85):
        self.threshold = similarity_threshold
        self.asked: list[tuple[str, str, list[float]]] = []  # (question, answer, embedding)

    def _embed(self, text: str) -> list[float]:
        import voyageai  # pip install voyageai
        vo = voyageai.Client()
        result = vo.embed([text], model="voyage-3-lite")
        return result.embeddings[0]

    def is_duplicate(self, question: str) -> tuple[bool, str | None]:
        if not self.asked:
            return False, None
        q_vec = self._embed(question)
        for prev_q, prev_a, prev_vec in self.asked:
            sim = cosine_similarity(q_vec, prev_vec)
            if sim >= self.threshold:
                return True, prev_a
        return False, None

    def record(self, question: str, answer: str) -> None:
        vec = self._embed(question)
        self.asked.append((question, answer, vec))

deduplicator = SemanticQuestionDeduplicator(similarity_threshold=0.85)

def run_with_dedup(task: str, user_responses: list[str]) -> str:
    messages = [{"role": "user", "content": task}]
    response_iter = iter(user_responses)

    # Build answered context string
    def answered_context() -> str:
        if not deduplicator.asked:
            return ""
        lines = ["Previously clarified (do not ask again):"]
        for q, a, _ in deduplicator.asked:
            lines.append(f"  Q: {q}")
            lines.append(f"  A: {a}")
        return "\n".join(lines)

    for turn in range(10):
        context = answered_context()
        system = "You are a helpful assistant." + (f"\n\n{context}" if context else "")

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        if "?" not in reply:
            return reply

        # Check for semantic duplicate
        is_dup, prev_answer = deduplicator.is_duplicate(reply)
        if is_dup:
            print(f"[Turn {turn+1}] Semantic duplicate detected! Injecting previous answer.")
            messages.append({"role": "user", "content": f"As mentioned: {prev_answer}"})
            continue

        # New question — get user answer
        try:
            answer = next(response_iter)
        except StopIteration:
            answer = "Please decide for me."

        deduplicator.record(reply, answer)
        messages.append({"role": "user", "content": answer})
        print(f"[Turn {turn+1}] New Q answered and recorded.")

    return "Max turns"

result = run_with_dedup(
    "Help me set up a monitoring system",
    ["Python", "We use Prometheus and Grafana", "Alert on p99 latency > 500ms"],
)
print(result[:200])
```

**Expected Token Savings:** Semantic dedup catches rephrased duplicates that string matching misses. Each caught loop: saves 2-4 turns × 400 tokens = 800-1,600 tokens. Embedding cost: ~1 API call per question (~0.001 USD) — far cheaper than the LLM turns saved.
**Environment:** Requires `voyageai` or `sentence-transformers` for embeddings. Threshold 0.85 works for most cases; lower to 0.75 if you want to catch more paraphrases. Use local embedding model if latency is critical.

---

### Option 5: Maximum Clarifications Hard Cap

Enforce a hard limit on the number of clarification questions per task. After the cap, proceed with stated assumptions.

```python
import anthropic
import re

client = anthropic.Anthropic()

MAX_CLARIFICATIONS = 2  # Never ask more than this many questions per task

def run_with_cap(task: str, user_responses: list[str]) -> str:
    messages = [{"role": "user", "content": task}]
    response_iter = iter(user_responses)
    clarifications_used = 0

    for turn in range(12):
        remaining = MAX_CLARIFICATIONS - clarifications_used
        system = (
            "You are a helpful assistant completing a task.\n\n"
            f"Clarification budget: {remaining}/{MAX_CLARIFICATIONS} questions remaining.\n"
        )
        if remaining == 0:
            system += (
                "CLARIFICATION BUDGET EXHAUSTED. Do NOT ask any questions. "
                "State your assumptions and proceed with the task immediately."
            )
        elif remaining == 1:
            system += "This is your LAST clarification question. Make it count, then proceed."

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        questions = re.findall(r"[^.!]*\?", reply)
        if questions and clarifications_used < MAX_CLARIFICATIONS:
            clarifications_used += 1
            print(f"[Turn {turn+1}] Clarification {clarifications_used}/{MAX_CLARIFICATIONS}: {questions[0].strip()!r}")
            try:
                answer = next(response_iter)
            except StopIteration:
                answer = "Use your best judgment."
            messages.append({"role": "user", "content": answer})
        elif questions and clarifications_used >= MAX_CLARIFICATIONS:
            print(f"[Turn {turn+1}] Agent tried to ask but budget exhausted — forcing proceed")
            messages.append({
                "role": "user",
                "content": "Budget exhausted. State your assumptions and complete the task now.",
            })
        else:
            print(f"[Turn {turn+1}] Proceeding with task (no question)")
            return reply

    return "Max turns"

result = run_with_cap(
    "Set up a CI/CD pipeline for my project",
    ["GitHub Actions", "Docker containers"],
)
print(f"\nResult: {result[:300]}")
```

**Expected Token Savings:** Hard cap of 2 questions saves the N-2 extra clarification turns the model would otherwise generate. For a typical 5-question loop: saves 3 turns × 400 tokens = 1,200 tokens. System prompt overhead: ~100 tokens. Net: 1,100 tokens per session.
**Environment:** Tune `MAX_CLARIFICATIONS` based on task complexity. For simple tasks (1-2 fields to clarify), use 1. For complex system design, use 3-4. The budget injection in the system prompt is key — the model responds well to explicit numeric limits.

---

### Option 6: Assumption-Forward Default — Proceed Unless Blocking

Flip the clarification strategy: the agent proceeds with stated assumptions by default and only asks when it is completely blocked.

```python
import anthropic

client = anthropic.Anthropic()

ASSUMPTION_FORWARD_SYSTEM = """You are a decisive, action-oriented assistant.

## Your approach:
1. When requirements are ambiguous, MAKE A REASONABLE ASSUMPTION and state it explicitly
2. Proceed immediately with the task
3. Ask a clarifying question ONLY if you are completely blocked (cannot proceed at all without the answer)
4. Never ask "just to confirm" or "could you clarify" — if you can make a reasonable guess, do it

## Format for assumptions:
"Assuming [X], I'll [proceed]. Let me know if you prefer something different."

## When you ARE allowed to ask:
- You genuinely cannot proceed without the answer (e.g., you don't know which system to integrate with)
- NOT when you could make a reasonable assumption

## Examples:
Instead of: "What language should I use?"
Say: "I'll use Python (common choice for this type of task). Adjust if needed."

Instead of: "What format should the output be?"
Say: "I'll output JSON — easy to parse downstream. Let me know if you need CSV instead."
"""

def run_assumption_forward(task: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ASSUMPTION_FORWARD_SYSTEM,
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

# Comparison table
"""
| Approach | Loop Prevention | User Experience | Complexity | Token Cost |
|---|---|---|---|---|
| Option 1: Clarification tracker | High | Good | Low | ~80 tokens/turn |
| Option 2: One-question policy | High | Good | Low | ~250 tokens once |
| Option 3: State machine | Very High | Structured | High | ~200 tokens/turn |
| Option 4: Semantic dedup | Very High | Seamless | Medium | +embedding call |
| Option 5: Hard cap | High | Direct | Low | ~100 tokens/turn |
| Option 6: Assumption-forward | Very High | Fastest | Low | ~200 tokens once |
"""

# Test cases
tasks = [
    "Write a function to parse dates from log files",
    "Set up authentication for my web app",
    "Create a script to backup my database",
]

for task in tasks:
    print(f"Task: {task!r}")
    result = run_assumption_forward(task)
    print(f"Response: {result[:200]}...\n")
```

**Expected Token Savings:** Assumption-forward approach eliminates clarification turns entirely for most tasks. Zero turns spent on Q&A × 400 tokens/turn = direct savings proportional to how often the model would have asked. For a 3-question task done assumption-forward: saves 6 turns (3Q + 3A) × 300 tokens = 1,800 tokens.
**Environment:** Best for technical tasks with reasonable defaults. Not suitable for high-stakes tasks (medical, legal, financial) where wrong assumptions have real costs. Use cap (Option 5) or policy (Option 2) for those domains instead.
