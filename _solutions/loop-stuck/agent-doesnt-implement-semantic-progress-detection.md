---
layout: solution
title: "Agent Doesn't Implement Semantic Progress Detection"
category: loop-stuck
description: "How to detect when an agent is making no meaningful progress toward its goal — even when it's still executing tool calls — and trigger recovery before wasting tokens on futile loops."
tags: [loop-stuck, progress, detection, watchdog, semantic, recovery]
---

# Agent Doesn't Implement Semantic Progress Detection

An agent can appear busy while making zero progress: calling tools that return the same empty result, rephrasing the same question, or bouncing between the same two states. Infinite loop detection based on step counts or repeated tool names misses these cases. Semantic progress detection compares the agent's state across iterations to determine whether it's actually advancing toward its goal.

## Option 1: Output Similarity Hashing — Detect Repeated Responses

Hash each model response and flag when the same content appears multiple times, indicating the agent is cycling.

```python
import anthropic
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResponseHashTracker:
    window_size: int = 5           # How many recent responses to track
    repetition_threshold: int = 2  # How many times same hash triggers alert
    recent_hashes: list = field(default_factory=list)
    hash_counts: Counter = field(default_factory=Counter)

    def _hash_response(self, text: str) -> str:
        # Normalize whitespace and lowercase before hashing
        normalized = " ".join(text.lower().split())
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def check(self, response_text: str) -> tuple[bool, Optional[str]]:
        """Returns (is_stuck, reason). is_stuck=True means cycling detected."""
        h = self._hash_response(response_text)
        self.hash_counts[h] += 1
        self.recent_hashes.append(h)

        if len(self.recent_hashes) > self.window_size:
            old = self.recent_hashes.pop(0)
            self.hash_counts[old] -= 1
            if self.hash_counts[old] == 0:
                del self.hash_counts[old]

        count = self.hash_counts[h]
        if count >= self.repetition_threshold:
            return True, f"Response repeated {count}x in last {self.window_size} turns (hash={h})"
        return False, None


def run_agent_with_repetition_detection(
    goal: str,
    max_steps: int = 10,
) -> str:
    client = anthropic.Anthropic()
    tracker = ResponseHashTracker(window_size=4, repetition_threshold=2)

    messages = [{"role": "user", "content": goal}]
    step = 0

    while step < max_steps:
        step += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=messages,
        )

        output = response.content[0].text
        is_stuck, reason = tracker.check(output)

        print(f"[Step {step}] {output[:60]}...")

        if is_stuck:
            print(f"[STUCK DETECTED] {reason}")
            # Recovery: inject progress prompt
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": (
                "You appear to be repeating yourself. Please take a different approach "
                "or explicitly state what information you still need to make progress."
            )})
            tracker.hash_counts.clear()  # Reset after recovery injection
            continue

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn":
            break

    return output


if __name__ == "__main__":
    result = run_agent_with_repetition_detection(
        "Search for the current CEO of Anthropic and summarize their background.",
        max_steps=8,
    )
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: 60-80% reduction on stuck loops — breaks cycles at step 2-3 instead of running to max_steps
# Environment: Autonomous agents, research pipelines where the model may loop on ambiguous goals
```

## Option 2: Goal Proximity Score — Track Distance to Objective

After each step, estimate how close the agent is to its stated goal. If the score stops increasing over N steps, declare stuck.

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProgressMeasurement:
    step: int
    score: float           # 0.0 = no progress, 1.0 = goal complete
    rationale: str
    delta: float = 0.0     # Change from previous score


@dataclass
class GoalProximityTracker:
    goal: str
    stagnation_threshold: int = 3   # Steps with no improvement before flagging stuck
    min_delta: float = 0.05         # Minimum score improvement to count as progress
    history: list = field(default_factory=list)

    def measure_progress(self, step: int, accumulated_output: str) -> ProgressMeasurement:
        """Use cheap Haiku call to score progress toward goal."""
        client = anthropic.Anthropic()

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": f"""Rate progress toward this goal from 0.0 to 1.0.

Goal: {self.goal}

Work done so far:
{accumulated_output[-500:]}

Reply with JSON: {{"score": 0.0-1.0, "rationale": "one sentence"}}"""}],
        )

        text = response.content[0].text
        try:
            json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
            data = __import__("json").loads(json_match.group()) if json_match else {}
            score = float(data.get("score", 0.5))
            rationale = data.get("rationale", "")
        except Exception:
            score = 0.5
            rationale = "score parse failed"

        prev_score = self.history[-1].score if self.history else 0.0
        measurement = ProgressMeasurement(
            step=step,
            score=score,
            rationale=rationale,
            delta=score - prev_score,
        )
        self.history.append(measurement)
        return measurement

    def is_stuck(self) -> tuple[bool, str]:
        if len(self.history) < self.stagnation_threshold:
            return False, ""
        recent = self.history[-self.stagnation_threshold:]
        if all(m.delta < self.min_delta for m in recent):
            avg_score = sum(m.score for m in recent) / len(recent)
            return True, (
                f"No meaningful progress in {self.stagnation_threshold} steps "
                f"(avg score={avg_score:.2f}, min_delta={self.min_delta})"
            )
        return False, ""


def run_with_progress_tracking(goal: str, max_steps: int = 8) -> str:
    client = anthropic.Anthropic()
    tracker = GoalProximityTracker(goal=goal, stagnation_threshold=3)

    messages = [{"role": "user", "content": goal}]
    accumulated = ""
    step = 0

    while step < max_steps:
        step += 1

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=messages,
        )
        output = response.content[0].text
        accumulated += f"\n[Step {step}]: {output}"

        measurement = tracker.measure_progress(step, accumulated)
        print(f"[Step {step}] Score={measurement.score:.2f} Δ={measurement.delta:+.2f} | {measurement.rationale}")

        stuck, reason = tracker.is_stuck()
        if stuck:
            print(f"[STUCK] {reason}")
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": (
                f"Progress has stalled. Current goal: '{goal}'. "
                "Please identify specifically what's blocking progress and try a fundamentally different approach."
            )})
            tracker.history.clear()
            continue

        messages.append({"role": "assistant", "content": output})

        if measurement.score >= 0.95:
            print(f"[COMPLETE] Goal achieved at step {step}")
            break

        if response.stop_reason == "end_turn" and measurement.score > 0.7:
            break

    return accumulated


if __name__ == "__main__":
    result = run_with_progress_tracking(
        "Find three concrete examples of transformer attention mechanism applications in production systems.",
        max_steps=6,
    )
    print(f"\nResult summary: {result[:300]}")

# Expected Token Savings: 40-65% by detecting stagnation early and injecting targeted recovery prompts
# Environment: Research agents, multi-step planners where fuzzy goals require semantic progress measurement
```

## Option 3: Tool Call Fingerprint — Detect Semantically Identical Calls

Two tool calls with different arguments can be semantically identical (e.g., "search: AI safety" vs "search: artificial intelligence safety"). Fingerprint tool calls by normalized semantic content.

```python
import anthropic
import re
import json
from dataclasses import dataclass, field
from collections import defaultdict


def normalize_tool_input(tool_name: str, input_data: dict) -> str:
    """Create a normalized fingerprint for a tool call."""
    # Lowercase and sort all string values
    normalized = {}
    for k, v in input_data.items():
        if isinstance(v, str):
            # Remove common filler words, lowercase, strip punctuation
            cleaned = re.sub(r"[^\w\s]", "", v.lower())
            words = [w for w in cleaned.split() if w not in
                     {"the", "a", "an", "in", "of", "for", "to", "and", "or", "is", "are"}]
            normalized[k] = " ".join(sorted(words))  # Sort for order-independence
        else:
            normalized[k] = v

    return f"{tool_name}|{json.dumps(normalized, sort_keys=True)}"


@dataclass
class ToolFingerprintTracker:
    repetition_limit: int = 2
    fingerprints: dict = field(default_factory=lambda: defaultdict(int))
    call_log: list = field(default_factory=list)

    def record_and_check(self, tool_name: str, input_data: dict) -> tuple[bool, str]:
        fp = normalize_tool_input(tool_name, input_data)
        self.fingerprints[fp] += 1
        self.call_log.append((tool_name, input_data, fp))

        count = self.fingerprints[fp]
        if count > self.repetition_limit:
            return True, f"Tool '{tool_name}' called {count}x with semantically identical input (fp={fp[:30]})"
        return False, ""

    def summary(self) -> list[str]:
        repeated = [(fp, count) for fp, count in self.fingerprints.items() if count > 1]
        return [f"{fp[:40]}: {count}x" for fp, count in sorted(repeated, key=lambda x: -x[1])]


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Simulate tool execution."""
    return f"[Result of {tool_name}({json.dumps(tool_input)[:60]})]"


def agent_with_fingerprint_detection(goal: str, max_steps: int = 10) -> str:
    client = anthropic.Anthropic()
    tracker = ToolFingerprintTracker(repetition_limit=2)

    tools = [
        {
            "name": "web_search",
            "description": "Search the web",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "read_document",
            "description": "Read a document by URL",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    ]

    messages = [{"role": "user", "content": goal}]
    step = 0

    while step < max_steps:
        step += 1
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        repeated_calls = []

        for block in response.content:
            if block.type == "tool_use":
                is_repeated, reason = tracker.record_and_check(block.name, block.input)
                if is_repeated:
                    print(f"[FINGERPRINT] {reason}")
                    repeated_calls.append(block.id)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            f"[STUCK DETECTION] This tool call is semantically identical to a previous call. "
                            f"The result will be the same. Try a different search query or approach."
                        ),
                        "is_error": True,
                    })
                else:
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        elif response.stop_reason == "end_turn":
            final = next((b.text for b in response.content if hasattr(b, "text")), "")
            print(f"\nRepeated call summary: {tracker.summary()}")
            return final

    return "[Max steps reached]"


if __name__ == "__main__":
    result = agent_with_fingerprint_detection(
        "Find information about the Anthropic Claude model release dates.",
        max_steps=6,
    )
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: 50-70% by short-circuiting redundant tool call chains that would yield identical results
# Environment: Research agents with web search tools, any agent with external API calls
```

## Option 4: State Snapshot Diffing — Detect No New Information Gained

After each agent cycle, snapshot the known state (facts gathered). If the state diff is empty for N steps, the agent is stuck.

```python
import anthropic
import json
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KnowledgeSnapshot:
    step: int
    facts: set              # Normalized fact strings
    new_facts: int = 0      # Facts added since previous snapshot

    def diff(self, previous: "KnowledgeSnapshot") -> set:
        return self.facts - previous.facts


@dataclass
class StateSnapshotTracker:
    stagnation_limit: int = 2
    snapshots: list = field(default_factory=list)

    def extract_facts(self, text: str) -> set:
        """Extract key facts from text as normalized strings."""
        # Simple heuristic: sentences ending with period or that start with known patterns
        sentences = re.split(r"[.!?]\s+", text)
        facts = set()
        for s in sentences:
            s = s.strip().lower()
            if len(s) > 20:  # Skip very short fragments
                # Normalize: remove articles, normalize whitespace
                normalized = re.sub(r"\b(the|a|an|is|are|was|were)\b", "", s)
                normalized = " ".join(normalized.split())
                facts.add(normalized[:80])  # Cap length
        return facts

    def snapshot(self, step: int, accumulated_text: str) -> KnowledgeSnapshot:
        all_facts = self.extract_facts(accumulated_text)
        prev_facts = self.snapshots[-1].facts if self.snapshots else set()
        new = all_facts - prev_facts

        snap = KnowledgeSnapshot(step=step, facts=all_facts, new_facts=len(new))
        self.snapshots.append(snap)
        return snap

    def is_stagnant(self) -> tuple[bool, str]:
        if len(self.snapshots) < self.stagnation_limit + 1:
            return False, ""
        recent = self.snapshots[-(self.stagnation_limit):]
        if all(s.new_facts == 0 for s in recent):
            return True, f"No new facts gained in {self.stagnation_limit} consecutive steps"
        return False, ""


def agent_with_state_diffing(goal: str, max_steps: int = 8) -> str:
    client = anthropic.Anthropic()
    tracker = StateSnapshotTracker(stagnation_limit=2)

    messages = [{"role": "user", "content": goal}]
    accumulated_text = goal
    step = 0

    while step < max_steps:
        step += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=messages,
        )
        output = response.content[0].text
        accumulated_text += "\n" + output

        snap = tracker.snapshot(step, accumulated_text)
        print(f"[Step {step}] New facts: {snap.new_facts} | Total facts: {len(snap.facts)}")

        stagnant, reason = tracker.is_stagnant()
        if stagnant:
            print(f"[STAGNANT] {reason} — injecting recovery")
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": (
                "You've stopped gathering new information. What specific data point or "
                "action would most advance progress? Take one concrete step now."
            )})
            # Reset to allow fresh progress measurement
            for s in tracker.snapshots[-2:]:
                s.new_facts = 1  # Artificially unblock to allow next cycle
            continue

        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn":
            break

    return output


if __name__ == "__main__":
    result = agent_with_state_diffing(
        "Summarize the key capabilities of the Claude claude-opus-4-6 model.",
        max_steps=5,
    )
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: 45-70% by detecting information saturation — agent stops gathering what it already knows
# Environment: Research agents, document analysis pipelines, RAG agents prone to re-retrieving known context
```

## Option 5: Watchdog Timer with Heartbeat — Wall-Clock Progress Gate

Run a background watchdog that expects the agent to report progress within a time window. If no heartbeat arrives, inject a recovery prompt.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class WatchdogConfig:
    heartbeat_timeout_seconds: float = 30.0   # Max seconds between heartbeats
    max_total_seconds: float = 180.0           # Hard kill after this long
    recovery_prompt: str = (
        "You have been running for too long without visible progress. "
        "Please provide an immediate partial result or explicitly state what's blocking you."
    )


class ProgressWatchdog:
    def __init__(self, config: WatchdogConfig):
        self.config = config
        self._last_heartbeat: float = time.monotonic()
        self._start_time: float = time.monotonic()
        self._should_stop: bool = False
        self._recovery_triggered: bool = False

    def heartbeat(self, label: str = ""):
        self._last_heartbeat = time.monotonic()
        elapsed = self._last_heartbeat - self._start_time
        print(f"[WATCHDOG] Heartbeat: {label} (elapsed={elapsed:.1f}s)")

    def seconds_since_heartbeat(self) -> float:
        return time.monotonic() - self._last_heartbeat

    def total_elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def check(self) -> tuple[bool, str]:
        """Returns (should_recover, reason)."""
        since_hb = self.seconds_since_heartbeat()
        elapsed = self.total_elapsed()

        if elapsed > self.config.max_total_seconds:
            return True, f"Hard timeout: {elapsed:.0f}s > {self.config.max_total_seconds}s limit"

        if since_hb > self.config.heartbeat_timeout_seconds:
            return True, f"No heartbeat for {since_hb:.0f}s > {self.config.heartbeat_timeout_seconds}s limit"

        return False, ""


async def async_agent_with_watchdog(goal: str, config: WatchdogConfig) -> str:
    client = anthropic.AsyncAnthropic()
    watchdog = ProgressWatchdog(config)

    messages = [{"role": "user", "content": goal}]
    step = 0
    max_steps = 8

    while step < max_steps:
        step += 1

        # Check watchdog before each step
        should_recover, reason = watchdog.check()
        if should_recover:
            print(f"[WATCHDOG TRIGGERED] {reason}")
            messages.append({"role": "user", "content": config.recovery_prompt})
            watchdog._last_heartbeat = time.monotonic()  # Reset after injection

        try:
            # Run with per-step timeout
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    messages=messages,
                ),
                timeout=config.heartbeat_timeout_seconds,
            )
        except asyncio.TimeoutError:
            print(f"[WATCHDOG] Step {step} timed out after {config.heartbeat_timeout_seconds}s")
            messages.append({"role": "user", "content": config.recovery_prompt})
            watchdog.heartbeat("timeout-recovery")
            continue

        output = response.content[0].text
        watchdog.heartbeat(f"step-{step}-complete")

        messages.append({"role": "assistant", "content": output})
        print(f"[Step {step}] {output[:80]}...")

        if response.stop_reason == "end_turn":
            break

    return messages[-1].get("content", "") if isinstance(messages[-1], dict) else str(messages[-1])


async def main():
    config = WatchdogConfig(
        heartbeat_timeout_seconds=20.0,
        max_total_seconds=90.0,
    )
    result = await async_agent_with_watchdog(
        "Explain the key differences between supervised and unsupervised learning.",
        config,
    )
    print(f"\nResult: {result[:200] if isinstance(result, str) else str(result)[:200]}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Prevents runaway agents — hard caps total token consumption per session
# Environment: Production agents with SLA requirements, interactive agents where user is waiting
```

## Option 6: LLM Self-Assessment — Ask the Agent if It's Stuck

Periodically inject a meta-question asking the agent to rate its own progress and declare if it needs help.

```python
import anthropic
import json
import re
from dataclasses import dataclass, field


@dataclass
class SelfAssessment:
    step: int
    progress_score: float       # 0.0–1.0
    is_stuck: bool
    blocker: str
    next_action: str


SELF_ASSESSMENT_PROMPT = """Without responding to the original task, assess your current progress.

Original goal: {goal}

Current step: {step}

Please respond with JSON only:
{{
  "progress_score": 0.0-1.0,
  "is_stuck": true/false,
  "blocker": "what is preventing progress, or 'none'",
  "next_action": "what you plan to do next"
}}"""


def self_assess(client: anthropic.Anthropic, goal: str, conversation: list, step: int) -> SelfAssessment:
    assessment_messages = conversation + [{
        "role": "user",
        "content": SELF_ASSESSMENT_PROMPT.format(goal=goal, step=step),
    }]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=120,
        messages=assessment_messages,
    )

    text = response.content[0].text
    try:
        json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
        data = json.loads(json_match.group()) if json_match else {}
        return SelfAssessment(
            step=step,
            progress_score=float(data.get("progress_score", 0.5)),
            is_stuck=bool(data.get("is_stuck", False)),
            blocker=data.get("blocker", "none"),
            next_action=data.get("next_action", "continue"),
        )
    except Exception:
        return SelfAssessment(step=step, progress_score=0.5, is_stuck=False,
                              blocker="none", next_action="continue")


def agent_with_self_assessment(
    goal: str,
    assess_every: int = 3,
    max_steps: int = 12,
) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": goal}]
    step = 0
    consecutive_stuck = 0

    while step < max_steps:
        step += 1

        # Periodic self-assessment
        if step % assess_every == 0:
            assessment = self_assess(client, goal, messages, step)
            print(
                f"[SELF-ASSESS step={step}] score={assessment.progress_score:.2f} "
                f"stuck={assessment.is_stuck} blocker={assessment.blocker[:40]}"
            )

            if assessment.is_stuck:
                consecutive_stuck += 1
                print(f"[RECOVERY] Agent self-reports stuck (×{consecutive_stuck}) | Next: {assessment.next_action}")

                if consecutive_stuck >= 2:
                    # Escalate: inject strong recovery
                    messages.append({"role": "user", "content": (
                        f"You've self-reported being stuck multiple times. "
                        f"The blocker is: '{assessment.blocker}'. "
                        "Please either produce the best partial answer you can, "
                        "or explicitly state what external information would unblock you."
                    )})
                    consecutive_stuck = 0
                else:
                    messages.append({"role": "user", "content": (
                        f"You identified: '{assessment.blocker}'. "
                        f"Try: {assessment.next_action}"
                    )})
            else:
                consecutive_stuck = 0

                if assessment.progress_score >= 0.9:
                    print(f"[COMPLETE] Self-assessed complete at step {step}")
                    break

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=messages,
        )
        output = response.content[0].text
        print(f"[Step {step}] {output[:70]}...")
        messages.append({"role": "assistant", "content": output})

        if response.stop_reason == "end_turn" and step >= assess_every:
            break

    return output


if __name__ == "__main__":
    result = agent_with_self_assessment(
        "Compile a list of 5 open-source vector databases with their key features.",
        assess_every=2,
        max_steps=8,
    )
    print(f"\nFinal result: {result[:300]}")

# Expected Token Savings: 35-60% by catching stuck states through metacognition before they consume many more steps
# Environment: Long-running autonomous agents, AI research assistants, agents with ambiguous multi-step goals
```

## Comparison

| Option | Detection Method | LLM Overhead | Latency | Best For |
|--------|-----------------|--------------|---------|----------|
| 1 Response Hash | MD5 of normalized output | None | ~0ms | Detecting verbatim response cycling |
| 2 Goal Proximity Score | Haiku progress scorer | 1 Haiku/step | ~300ms | Fuzzy goals with semantic progress |
| 3 Tool Fingerprinting | Semantic input normalization | None | ~1ms | Agents with external tool calls |
| 4 State Snapshot Diff | Fact extraction + set diff | None | ~5ms | Research agents accumulating knowledge |
| 5 Watchdog Timer | Wall-clock heartbeat | None | ~0ms | Production agents with hard time SLAs |
| 6 LLM Self-Assessment | Haiku metacognitive check | 1 Haiku/N steps | ~300ms | Autonomous agents with complex goals |
