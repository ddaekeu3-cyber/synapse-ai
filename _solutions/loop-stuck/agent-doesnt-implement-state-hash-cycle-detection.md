---
layout: solution
title: "Agent Doesn't Implement State Hash Cycle Detection"
category: loop-stuck
description: "Detect when an agent revisits identical states by hashing conversation context, tool inputs, or working memory—breaking infinite cycles before they consume budget."
tags: [loop-detection, cycle-detection, hashing, state-tracking, infinite-loop]
---

# Agent Doesn't Implement State Hash Cycle Detection

## Problem

Agents can enter cycles where they repeat the same tool calls with identical arguments, produce identical intermediate outputs, or oscillate between two states—burning tokens until max_tokens is hit with no progress made.

## Solution Options

### Option 1: Message History Hash Deduplication

```python
import anthropic
import hashlib
import json

client = anthropic.Anthropic()

def hash_messages(messages: list[dict]) -> str:
    """Hash the last N messages to detect repeated conversation state."""
    serialized = json.dumps(messages[-6:], sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]

def run_with_cycle_detection(initial_prompt: str, max_turns: int = 10) -> str:
    messages = [{"role": "user", "content": initial_prompt}]
    seen_hashes: set[str] = set()
    cycle_streak = 0
    MAX_CYCLE_STREAK = 2

    for turn in range(max_turns):
        state_hash = hash_messages(messages)

        if state_hash in seen_hashes:
            cycle_streak += 1
            print(f"[Turn {turn+1}] CYCLE DETECTED (hash={state_hash}, streak={cycle_streak})")
            if cycle_streak >= MAX_CYCLE_STREAK:
                messages.append({
                    "role": "user",
                    "content": "You appear to be repeating yourself. Summarize what you have found so far and give a final answer."
                })
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=messages
                )
                return resp.content[0].text
        else:
            cycle_streak = 0
        seen_hashes.add(state_hash)

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages
        )
        reply = resp.content[0].text
        messages.append({"role": "assistant", "content": reply})

        if resp.stop_reason == "end_turn" and len(reply) < 50:
            return reply

        # Simulate continued exploration
        messages.append({"role": "user", "content": "Continue your analysis."})
        print(f"[Turn {turn+1}] hash={state_hash} reply_len={len(reply)}")

    return messages[-1]["content"]

result = run_with_cycle_detection(
    "Analyze the tradeoffs between microservices and monoliths. Think step by step."
)
print(f"\nFinal: {result[:200]}")

# Expected Token Savings: breaks cycles before max_turns; saves 30-80% of wasted tokens
# Environment: iterative reasoning agents, ReAct loops, multi-step planners
```

### Option 2: Tool Call Argument Hash to Detect Repeated Tool Use

```python
import anthropic
import hashlib
import json
from collections import Counter

client = anthropic.Anthropic()

def hash_tool_call(tool_name: str, tool_input: dict) -> str:
    payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()[:12]

def run_tool_cycle_guard(user_msg: str, max_iterations: int = 8) -> str:
    tool_call_hashes: list[str] = []
    call_counter: Counter = Counter()

    messages = [{"role": "user", "content": user_msg}]

    tools = [{
        "name": "search",
        "description": "Search for information on a topic",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    }]

    for iteration in range(max_iterations):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        if resp.stop_reason != "tool_use":
            break

        tool_results = []
        cycle_forced_exit = False

        for block in resp.content:
            if block.type != "tool_use":
                continue

            call_hash = hash_tool_call(block.name, block.input)
            call_counter[call_hash] += 1

            if call_counter[call_hash] > 2:
                print(f"[Iter {iteration+1}] TOOL CYCLE: {block.name}({block.input}) called {call_counter[call_hash]}x")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "ERROR: This exact query was already searched. You must synthesize from existing results and give a final answer now."
                })
                cycle_forced_exit = True
            else:
                tool_call_hashes.append(call_hash)
                # Simulate search result
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Search results for '{block.input.get('query', '')}': [simulated relevant data]"
                })
                print(f"[Iter {iteration+1}] Tool call #{call_counter[call_hash]}: {block.name}({block.input})")

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

        if cycle_forced_exit:
            # One final synthesis call
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages
            )
            return final.content[0].text

    return "Max iterations reached without resolution."

result = run_tool_cycle_guard("Research the history of distributed databases and summarize key milestones.")
print(f"\nResult: {result[:300]}")

# Expected Token Savings: ~40-60% by catching repeated tool calls at iteration 3 vs 8
# Environment: ReAct agents, autonomous research agents, multi-step tool-use loops
```

### Option 3: Working Memory State Hash with Rollback

```python
import anthropic
import hashlib
import json
from dataclasses import dataclass, field
from copy import deepcopy

client = anthropic.Anthropic()

@dataclass
class AgentWorkingMemory:
    facts: list[str] = field(default_factory=list)
    current_hypothesis: str = ""
    steps_taken: list[str] = field(default_factory=list)
    iteration: int = 0

    def to_hash(self) -> str:
        state = {
            "facts": sorted(self.facts),
            "hypothesis": self.current_hypothesis,
            "steps": self.steps_taken[-3:]  # last 3 steps for cycle detection
        }
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]

    def to_prompt_context(self) -> str:
        return (
            f"Current hypothesis: {self.current_hypothesis}\n"
            f"Known facts: {', '.join(self.facts[-5:]) or 'none'}\n"
            f"Steps taken: {', '.join(self.steps_taken[-3:]) or 'none'}"
        )

def run_memory_cycle_guard(problem: str, max_steps: int = 8) -> str:
    memory = AgentWorkingMemory()
    state_history: list[str] = []
    last_valid_memory: AgentWorkingMemory | None = None

    for step in range(max_steps):
        state_hash = memory.to_hash()

        if state_hash in state_history:
            print(f"[Step {step+1}] STATE CYCLE detected (hash={state_hash})")
            # Rollback to last valid state and inject escape instruction
            if last_valid_memory:
                memory = deepcopy(last_valid_memory)
            escape_prompt = (
                f"Problem: {problem}\n\n"
                f"{memory.to_prompt_context()}\n\n"
                "WARNING: You are stuck in a reasoning loop. "
                "Do NOT repeat previous steps. Try a completely different approach or admit uncertainty."
            )
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=384,
                messages=[{"role": "user", "content": escape_prompt}]
            )
            return resp.content[0].text

        state_history.append(state_hash)
        last_valid_memory = deepcopy(memory)

        prompt = (
            f"Problem: {problem}\n\n"
            f"{memory.to_prompt_context()}\n\n"
            "Take the next reasoning step. Output JSON: "
            '{"new_fact": "...", "updated_hypothesis": "...", "step_description": "...", "done": false}'
        )

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            import re
            match = re.search(r'\{.*\}', resp.content[0].text, re.DOTALL)
            parsed = json.loads(match.group()) if match else {}
            if parsed.get("new_fact"):
                memory.facts.append(parsed["new_fact"])
            if parsed.get("updated_hypothesis"):
                memory.current_hypothesis = parsed["updated_hypothesis"]
            if parsed.get("step_description"):
                memory.steps_taken.append(parsed["step_description"])
            memory.iteration = step + 1

            print(f"[Step {step+1}] hash={state_hash} step={parsed.get('step_description', '')[:60]}")

            if parsed.get("done"):
                return memory.current_hypothesis
        except Exception:
            memory.steps_taken.append(f"parse_error_step_{step}")

    return memory.current_hypothesis or "Could not reach conclusion."

solution = run_memory_cycle_guard("Why do distributed systems struggle with strong consistency at scale?")
print(f"\nConclusion: {solution[:300]}")

# Expected Token Savings: rollback prevents re-processing identical states; ~25% savings
# Environment: structured reasoning agents, problem-solving agents with explicit memory
```

### Option 4: Sliding Window Hash for Long-Running Agents

```python
import anthropic
import hashlib
import json
from collections import deque

client = anthropic.Anthropic()

class SlidingWindowCycleDetector:
    """Detects cycles using a sliding window over recent states."""

    def __init__(self, window_size: int = 5, max_repeats: int = 2):
        self.window_size = window_size
        self.max_repeats = max_repeats
        self.recent_hashes: deque[str] = deque(maxlen=window_size * 3)
        self.global_counts: dict[str, int] = {}

    def compute_hash(self, content: str) -> str:
        return hashlib.md5(content.strip().lower().encode()).hexdigest()[:12]

    def check_and_record(self, content: str) -> tuple[bool, str]:
        """Returns (is_cycle, hash). True if cycle detected."""
        h = self.compute_hash(content)
        self.recent_hashes.append(h)
        self.global_counts[h] = self.global_counts.get(h, 0) + 1

        # Check recent window for repeats
        recent = list(self.recent_hashes)[-self.window_size:]
        repeat_in_window = recent.count(h)
        global_repeat = self.global_counts[h]

        is_cycle = repeat_in_window >= self.max_repeats or global_repeat > 3
        return is_cycle, h

    def stats(self) -> dict:
        return {
            "window_size": len(self.recent_hashes),
            "unique_states": len(self.global_counts),
            "max_repeat": max(self.global_counts.values(), default=0)
        }

def long_running_agent(task: str, max_steps: int = 12) -> str:
    detector = SlidingWindowCycleDetector(window_size=4, max_repeats=2)
    messages = [{"role": "user", "content": task}]
    step_responses = []

    for step in range(max_steps):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages
        )
        reply = resp.content[0].text
        step_responses.append(reply)

        is_cycle, state_hash = detector.check_and_record(reply)

        if is_cycle:
            print(f"[Step {step+1}] SLIDING WINDOW CYCLE (hash={state_hash}) | Stats: {detector.stats()}")
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user",
                "content": (
                    "You are repeating outputs. This is your final step. "
                    "Synthesize everything into a definitive answer without repeating previous content."
                )
            })
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages
            )
            return final.content[0].text

        messages.append({"role": "assistant", "content": reply})

        if resp.stop_reason == "end_turn" and step > 1:
            return reply

        messages.append({
            "role": "user",
            "content": f"Step {step+2}: Continue refining your analysis."
        })
        print(f"[Step {step+1}] hash={state_hash} len={len(reply)}")

    return step_responses[-1] if step_responses else ""

result = long_running_agent("Iteratively analyze the pros and cons of event-driven architecture.")
print(f"\nFinal answer: {result[:200]}")

# Expected Token Savings: sliding window catches slow drifting cycles vs exact-repeat detector
# Environment: long autonomous tasks, iterative refinement loops, multi-day agent sessions
```

### Option 5: Structural Similarity Hash for Near-Duplicate Detection

```python
import anthropic
import hashlib
import re

client = anthropic.Anthropic()

def normalize_text(text: str) -> str:
    """Strip variable content (numbers, dates, IDs) before hashing."""
    text = re.sub(r'\b\d+\b', 'N', text)             # numbers -> N
    text = re.sub(r'\b[a-f0-9]{8,}\b', 'HASH', text)  # hex strings -> HASH
    text = re.sub(r'\s+', ' ', text.lower().strip())   # normalize whitespace
    return text

def structural_hash(text: str) -> str:
    """Hash normalized structure — catches near-duplicates, not just exact repeats."""
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]

def jaccard_similarity(a: str, b: str, n: int = 3) -> float:
    """Character n-gram Jaccard similarity between two strings."""
    def ngrams(s: str) -> set:
        return {s[i:i+n] for i in range(len(s)-n+1)}
    ng_a, ng_b = ngrams(normalize_text(a)), ngrams(normalize_text(b))
    if not ng_a or not ng_b:
        return 0.0
    return len(ng_a & ng_b) / len(ng_a | ng_b)

def run_with_similarity_guard(prompt: str, max_turns: int = 8, sim_threshold: float = 0.85) -> str:
    messages = [{"role": "user", "content": prompt}]
    prior_responses: list[str] = []

    for turn in range(max_turns):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            messages=messages
        )
        reply = resp.content[0].text

        # Check structural similarity against all prior responses
        max_sim = 0.0
        most_similar_turn = -1
        for i, prior in enumerate(prior_responses):
            sim = jaccard_similarity(reply, prior)
            if sim > max_sim:
                max_sim = sim
                most_similar_turn = i

        if max_sim >= sim_threshold:
            print(f"[Turn {turn+1}] NEAR-DUPLICATE (sim={max_sim:.3f} vs turn {most_similar_turn+1})")
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user",
                "content": f"Your response is {max_sim:.0%} similar to a previous response. Provide genuinely new information or conclude."
            })
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages
            )
            return final.content[0].text

        prior_responses.append(reply)
        messages.append({"role": "assistant", "content": reply})
        print(f"[Turn {turn+1}] max_sim={max_sim:.3f} (safe) len={len(reply)}")

        if resp.stop_reason == "end_turn":
            return reply

        messages.append({"role": "user", "content": "Continue your analysis."})

    return prior_responses[-1] if prior_responses else ""

run_with_similarity_guard("Explain database indexing strategies in depth. Be thorough.")

# Expected Token Savings: catches paraphrased cycles that exact hashing misses
# Environment: content generation loops, iterative essay refinement, explanation agents
```

### Option 6: Multi-Signal Cycle Score with Configurable Thresholds

```python
import anthropic
import hashlib
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class CycleSignal:
    exact_hash_repeat: bool
    normalized_hash_repeat: bool
    length_variance_low: bool
    keyword_repeat_high: bool

    @property
    def score(self) -> int:
        return sum([self.exact_hash_repeat, self.normalized_hash_repeat,
                    self.length_variance_low, self.keyword_repeat_high])

    def is_cycle(self, threshold: int = 2) -> bool:
        return self.score >= threshold

class MultiSignalCycleDetector:
    def __init__(self):
        self.exact_hashes: list[str] = []
        self.norm_hashes: list[str] = []
        self.lengths: list[int] = []
        self.keyword_sets: list[frozenset] = []

    def _exact_hash(self, text: str) -> str:
        return hashlib.md5(text.strip().encode()).hexdigest()[:10]

    def _norm_hash(self, text: str) -> str:
        normalized = re.sub(r'\s+', ' ', text.lower().strip())
        normalized = re.sub(r'\b\d+\b', '', normalized)
        return hashlib.md5(normalized.encode()).hexdigest()[:10]

    def _keywords(self, text: str) -> frozenset:
        words = re.findall(r'\b[a-z]{4,}\b', text.lower())
        from collections import Counter
        top = [w for w, _ in Counter(words).most_common(10)]
        return frozenset(top)

    def evaluate(self, response: str) -> CycleSignal:
        eh = self._exact_hash(response)
        nh = self._norm_hash(response)
        kw = self._keywords(response)
        ln = len(response)

        exact_repeat = eh in self.exact_hashes[-5:]
        norm_repeat = nh in self.norm_hashes[-5:]

        if len(self.lengths) >= 3:
            recent_lengths = self.lengths[-3:]
            variance = max(recent_lengths) - min(recent_lengths)
            length_flat = variance < 50  # less than 50 char variance
        else:
            length_flat = False

        if self.keyword_sets:
            overlaps = [len(kw & prior) / max(len(kw | prior), 1) for prior in self.keyword_sets[-3:]]
            keyword_repeat = any(o > 0.8 for o in overlaps)
        else:
            keyword_repeat = False

        self.exact_hashes.append(eh)
        self.norm_hashes.append(nh)
        self.lengths.append(ln)
        self.keyword_sets.append(kw)

        return CycleSignal(exact_repeat, norm_repeat, length_flat, keyword_repeat)

def run_multi_signal_guard(task: str, max_turns: int = 10, cycle_threshold: int = 2) -> str:
    detector = MultiSignalCycleDetector()
    messages = [{"role": "user", "content": task}]

    for turn in range(max_turns):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            messages=messages
        )
        reply = resp.content[0].text
        signal = detector.evaluate(reply)

        status = f"score={signal.score}/4 exact={signal.exact_hash_repeat} norm={signal.normalized_hash_repeat} flat_len={signal.length_variance_low} kw={signal.keyword_repeat_high}"
        print(f"[Turn {turn+1}] {status}")

        if signal.is_cycle(threshold=cycle_threshold):
            print(f"  -> CYCLE DETECTED (score >= {cycle_threshold})")
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user",
                "content": "Multiple cycle indicators detected. Give your final consolidated answer now."
            })
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages
            )
            return final.content[0].text

        messages.append({"role": "assistant", "content": reply})
        if resp.stop_reason == "end_turn" and turn > 1:
            return reply
        messages.append({"role": "user", "content": "Continue."})

    return messages[-2]["content"] if len(messages) >= 2 else ""

run_multi_signal_guard("Explain how neural networks learn, step by step, with increasing depth.")

# Expected Token Savings: multi-signal catches cycles that single-hash misses; ~35% average savings
# Environment: any iterative agent; especially valuable for long autonomous tasks
```

## Comparison

| Option | Detection Method | False Positive Risk | Catches Paraphrased Cycles |
|--------|-----------------|---------------------|---------------------------|
| 1 | Message history hash | Low | No |
| 2 | Tool call argument hash | Very low | No |
| 3 | Working memory state hash | Low | Partial |
| 4 | Sliding window hash | Low | No |
| 5 | Jaccard structural similarity | Medium | Yes |
| 6 | Multi-signal score (4 signals) | Very low | Yes |
