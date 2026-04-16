---
title: "Agent Doesn't Implement Semantic Drift Detection"
description: "Over long conversations or multi-step pipelines, agents drift away from the original goal without anyone noticing. Semantic drift detection compares current outputs against the original intent and triggers realignment before the agent goes too far off-course."
difficulty: advanced
category: observability
tags: [semantic-drift, goal-tracking, observability, alignment, multi-turn, long-context]
---

## Problem

In long multi-turn conversations or agentic pipelines, agents gradually drift from the original goal. Each step looks locally reasonable, but the cumulative shift is significant — the agent solving a "summarize quarterly results" task ends up writing a market analysis essay, or an agent asked to fix a bug starts refactoring unrelated code. Without drift detection, there is no signal until the user notices the final output is wrong.

```python
# BAD: no drift monitoring — agent silently wanders
async def run_pipeline(initial_goal: str, steps: int = 10):
    context = initial_goal
    for step in range(steps):
        response = await call_model(context)
        context = response  # goal drifts step by step, undetected
    return context
```

## Solution 1: Embedding-Based Cosine Drift Monitor

Embed the goal and each output; alert when cosine similarity drops below threshold.

```python
import asyncio
import math
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class DriftEvent:
    step: int
    similarity: float
    original_goal: str
    current_summary: str
    severity: str  # "warn" | "critical"

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x ** 2 for x in a))
    mag_b = math.sqrt(sum(x ** 2 for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)

async def get_embedding(text: str) -> list[float]:
    """Use Haiku to produce a pseudo-embedding via structured output."""
    # Real implementation: use a dedicated embedding API
    # Here we simulate with Haiku extracting key concepts as a feature vector
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Extract the top 10 key concepts from this text as a JSON array of strings. "
                f"Output ONLY the JSON array.\n\nText: {text[:500]}"
            )
        }]
    )
    import json
    try:
        concepts = json.loads(response.content[0].text.strip())
    except Exception:
        concepts = text.split()[:10]

    # Hash concepts into a fixed-size vector (simplified)
    vector = [0.0] * 64
    for i, concept in enumerate(concepts[:10]):
        for j, char in enumerate(concept[:6]):
            idx = (i * 7 + j * 3) % 64
            vector[idx] += ord(char) / 1000.0
    mag = math.sqrt(sum(x**2 for x in vector)) or 1.0
    return [x / mag for x in vector]

class SemanticDriftMonitor:
    def __init__(self, warn_threshold: float = 0.75, critical_threshold: float = 0.5):
        self.warn_threshold = warn_threshold
        self.critical_threshold = critical_threshold
        self.goal_embedding: list[float] | None = None
        self.drift_events: list[DriftEvent] = []

    async def set_goal(self, goal: str):
        self.goal_embedding = await get_embedding(goal)
        self.original_goal = goal

    async def check(self, step: int, output: str) -> DriftEvent | None:
        if not self.goal_embedding:
            return None
        output_embedding = await get_embedding(output)
        similarity = cosine_similarity(self.goal_embedding, output_embedding)

        if similarity < self.critical_threshold:
            severity = "critical"
        elif similarity < self.warn_threshold:
            severity = "warn"
        else:
            return None

        event = DriftEvent(
            step=step,
            similarity=round(similarity, 3),
            original_goal=self.original_goal,
            current_summary=output[:200],
            severity=severity
        )
        self.drift_events.append(event)
        return event

async def run_monitored_pipeline(goal: str, num_steps: int = 5) -> str:
    monitor = SemanticDriftMonitor(warn_threshold=0.7, critical_threshold=0.4)
    await monitor.set_goal(goal)

    messages = [{"role": "user", "content": goal}]
    output = ""

    for step in range(num_steps):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=messages
        )
        output = response.content[0].text if response.content else ""
        messages.append({"role": "assistant", "content": output})

        drift = await monitor.check(step, output)
        if drift:
            print(f"[Drift {drift.severity.upper()}] Step {step}: similarity={drift.similarity}")
            if drift.severity == "critical":
                # Inject realignment
                messages.append({
                    "role": "user",
                    "content": (
                        f"IMPORTANT: You have drifted from the original goal. "
                        f"Original goal: '{goal}'\n"
                        f"Please refocus your response on the original goal."
                    )
                })
            else:
                messages.append({"role": "user", "content": "Continue, staying focused on the original goal."})
        else:
            messages.append({"role": "user", "content": "Continue."})

    print(f"Drift events: {len(monitor.drift_events)}")
    return output

async def main():
    result = await run_monitored_pipeline(
        "Explain how to optimize database query performance with indexes",
        num_steps=4
    )
    print(result[:300])

asyncio.run(main())
```

## Solution 2: Goal Anchor with Periodic LLM Alignment Check

Ask the model itself to assess whether it's still on track.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

ALIGNMENT_CHECKER_PROMPT = """You are an alignment checker. Given an original goal and a recent agent output, assess whether the output is still aligned with the goal.

Respond with JSON only:
{
  "aligned": true/false,
  "alignment_score": 0.0-1.0,
  "drift_description": "brief description of any drift",
  "realignment_suggestion": "how to get back on track, or null if aligned"
}"""

@dataclass
class AlignmentCheck:
    step: int
    aligned: bool
    score: float
    drift_description: str
    realignment_suggestion: str | None

async def check_alignment(
    original_goal: str,
    recent_output: str,
    step: int
) -> AlignmentCheck:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=ALIGNMENT_CHECKER_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Original goal: {original_goal}\n\n"
                f"Recent output (step {step}):\n{recent_output[:600]}"
            )
        }]
    )
    text = response.content[0].text.strip()
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[start:end])
        return AlignmentCheck(
            step=step,
            aligned=data.get("aligned", True),
            score=float(data.get("alignment_score", 1.0)),
            drift_description=data.get("drift_description", ""),
            realignment_suggestion=data.get("realignment_suggestion")
        )
    except Exception:
        return AlignmentCheck(step, True, 1.0, "", None)

async def run_with_alignment_checks(
    goal: str,
    subtasks: list[str],
    check_every: int = 2
) -> list[str]:
    results: list[str] = []
    realignments = 0

    for i, subtask in enumerate(subtasks):
        prompt = subtask
        if results and (i % check_every == 0):
            check = await check_alignment(goal, results[-1], i)
            print(f"[Alignment] Step {i}: score={check.score:.2f}, aligned={check.aligned}")
            if not check.aligned and check.realignment_suggestion:
                print(f"[Drift] {check.drift_description}")
                prompt = (
                    f"REALIGNMENT REQUIRED: {check.realignment_suggestion}\n\n"
                    f"Original goal: {goal}\n\n"
                    f"Now: {subtask}"
                )
                realignments += 1

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        output = response.content[0].text if response.content else ""
        results.append(output)

    print(f"Total realignments: {realignments}/{len(subtasks)}")
    return results

async def main():
    results = await run_with_alignment_checks(
        goal="Write a Python tutorial on list comprehensions for beginners",
        subtasks=[
            "Introduce Python lists",
            "Explain what comprehensions are",
            "Show 3 examples of list comprehensions",
            "Summarize the key benefits"
        ],
        check_every=2
    )
    for i, r in enumerate(results):
        print(f"\n--- Step {i+1} ---\n{r[:200]}")

asyncio.run(main())
```

## Solution 3: Sliding Window Drift with Rolling Baseline

Compare recent output against both the original goal and a rolling window to detect gradual vs. sudden drift.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from collections import deque
from dataclasses import dataclass

client = AsyncAnthropic()

def keyword_overlap(text_a: str, text_b: str) -> float:
    words_a = set(re.findall(r"\b\w{4,}\b", text_a.lower()))
    words_b = set(re.findall(r"\b\w{4,}\b", text_b.lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)

@dataclass
class DriftMetrics:
    step: int
    goal_similarity: float        # vs. original goal
    window_similarity: float      # vs. rolling window average
    gradient: float               # rate of change vs previous step
    alert: str | None             # "goal_drift" | "sudden_shift" | None

class SlidingWindowDriftDetector:
    def __init__(
        self,
        original_goal: str,
        window_size: int = 3,
        goal_threshold: float = 0.15,
        gradient_threshold: float = 0.3
    ):
        self.original_goal = original_goal
        self.window_size = window_size
        self.goal_threshold = goal_threshold
        self.gradient_threshold = gradient_threshold
        self.window: deque[str] = deque(maxlen=window_size)
        self.previous_goal_sim: float | None = None

    def check(self, step: int, output: str) -> DriftMetrics:
        goal_sim = keyword_overlap(self.original_goal, output)

        if self.window:
            window_text = " ".join(self.window)
            window_sim = keyword_overlap(window_text, output)
        else:
            window_sim = 1.0

        gradient = 0.0
        if self.previous_goal_sim is not None:
            gradient = abs(goal_sim - self.previous_goal_sim)

        alert = None
        if goal_sim < self.goal_threshold:
            alert = "goal_drift"
        elif gradient > self.gradient_threshold:
            alert = "sudden_shift"

        self.window.append(output)
        self.previous_goal_sim = goal_sim

        return DriftMetrics(step, round(goal_sim, 3), round(window_sim, 3), round(gradient, 3), alert)

async def run_with_sliding_drift(goal: str, conversation_turns: int = 6) -> str:
    detector = SlidingWindowDriftDetector(goal, window_size=3)
    messages = [{"role": "user", "content": goal}]
    last_output = ""

    for turn in range(conversation_turns):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=messages
        )
        output = response.content[0].text if response.content else ""
        last_output = output
        metrics = detector.check(turn, output)

        status = f"goal_sim={metrics.goal_similarity}, gradient={metrics.gradient}"
        if metrics.alert:
            print(f"[DRIFT ALERT: {metrics.alert}] Turn {turn}: {status}")
            # Inject realignment prompt
            messages.append({"role": "assistant", "content": output})
            messages.append({
                "role": "user",
                "content": (
                    f"You appear to have drifted from the original goal.\n"
                    f"Original goal: '{goal}'\n"
                    f"Please bring your next response back to the original goal."
                )
            })
        else:
            print(f"[OK] Turn {turn}: {status}")
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": "Continue."})

    return last_output

async def main():
    result = await run_with_sliding_drift(
        "Explain the benefits of test-driven development",
        conversation_turns=4
    )
    print(result[:300])

asyncio.run(main())
```

## Solution 4: Structured Goal State Tracker

Track explicit goal decomposition and mark which sub-goals are still being addressed.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class SubGoal:
    id: str
    description: str
    addressed: bool = False
    last_addressed_step: int = -1
    coverage_score: float = 0.0

@dataclass
class GoalState:
    original_goal: str
    sub_goals: list[SubGoal]
    drift_warnings: list[str] = field(default_factory=list)

async def decompose_goal(goal: str) -> list[str]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Break this goal into 3-5 specific sub-goals as a JSON array of strings. "
                f"Output ONLY the JSON array.\n\nGoal: {goal}"
            )
        }]
    )
    text = response.content[0].text.strip()
    try:
        start, end = text.find("["), text.rfind("]") + 1
        return json.loads(text[start:end])
    except Exception:
        return [goal]

async def assess_sub_goal_coverage(
    sub_goal: str,
    output: str
) -> float:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"On a scale of 0.0 to 1.0, how well does this output address the sub-goal?\n"
                f"Sub-goal: {sub_goal}\n"
                f"Output: {output[:400]}\n\n"
                f"Output ONLY a decimal number like 0.7"
            )
        }]
    )
    text = response.content[0].text.strip()
    try:
        return float(re.search(r"[\d.]+", text).group())
    except Exception:
        return 0.5

import re

async def run_with_goal_state_tracking(goal: str, num_steps: int = 5) -> str:
    sub_goal_texts = await decompose_goal(goal)
    state = GoalState(
        original_goal=goal,
        sub_goals=[SubGoal(f"sg{i}", sg) for i, sg in enumerate(sub_goal_texts)]
    )

    print(f"[Goal] Decomposed into {len(state.sub_goals)} sub-goals:")
    for sg in state.sub_goals:
        print(f"  [{sg.id}] {sg.description}")

    messages = [{"role": "user", "content": goal}]
    last_output = ""

    for step in range(num_steps):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=messages
        )
        output = response.content[0].text if response.content else ""
        last_output = output

        # Check coverage of all sub-goals concurrently
        scores = await asyncio.gather(*[
            assess_sub_goal_coverage(sg.description, output)
            for sg in state.sub_goals
        ])

        for sg, score in zip(state.sub_goals, scores):
            sg.coverage_score = score
            if score > 0.5:
                sg.addressed = True
                sg.last_addressed_step = step

        unaddressed = [sg for sg in state.sub_goals if not sg.addressed]
        poorly_covered = [sg for sg in state.sub_goals if sg.coverage_score < 0.3]

        coverage_pct = (len(state.sub_goals) - len(unaddressed)) / len(state.sub_goals)
        print(f"[Step {step}] Goal coverage: {coverage_pct:.0%}, unaddressed: {len(unaddressed)}")

        messages.append({"role": "assistant", "content": output})

        if unaddressed and step < num_steps - 1:
            focus_hint = "; ".join(sg.description for sg in unaddressed[:2])
            messages.append({
                "role": "user",
                "content": f"Please also address: {focus_hint}"
            })
        elif step < num_steps - 1:
            messages.append({"role": "user", "content": "Continue and conclude."})

    final_coverage = sum(1 for sg in state.sub_goals if sg.addressed) / len(state.sub_goals)
    print(f"[Final] Sub-goal coverage: {final_coverage:.0%}")
    return last_output

async def main():
    result = await run_with_goal_state_tracking(
        "Write a guide on setting up a Python virtual environment",
        num_steps=3
    )
    print(result[:300])

asyncio.run(main())
```

## Solution 5: Dual-Agent Drift Watchdog

A dedicated watchdog agent monitors the primary agent's outputs in parallel.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class WatchdogVerdict:
    step: int
    on_track: bool
    confidence: float
    corrective_instruction: str | None

async def watchdog_check(
    goal: str,
    output: str,
    history_summary: str,
    step: int
) -> WatchdogVerdict:
    prompt = (
        f"You are a watchdog monitoring an AI agent for semantic drift.\n\n"
        f"Original goal: {goal}\n"
        f"Conversation summary so far: {history_summary[:300]}\n"
        f"Latest agent output: {output[:500]}\n\n"
        f"Is the agent still on track? Reply:\n"
        f"ON_TRACK if yes.\n"
        f"OFF_TRACK: <corrective instruction> if no."
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()

    if text.startswith("ON_TRACK"):
        return WatchdogVerdict(step, True, 0.9, None)
    elif "OFF_TRACK" in text:
        correction = text.split("OFF_TRACK:", 1)[-1].strip()
        return WatchdogVerdict(step, False, 0.8, correction or "Refocus on the original goal.")
    else:
        return WatchdogVerdict(step, True, 0.5, None)  # uncertain, assume ok

async def primary_agent_step(
    messages: list[dict],
    corrective: str | None = None
) -> str:
    msgs = list(messages)
    if corrective:
        msgs.append({"role": "user", "content": corrective})
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=msgs
    )
    return response.content[0].text if response.content else ""

async def run_with_watchdog(goal: str, steps: int = 5) -> str:
    messages = [{"role": "user", "content": goal}]
    history_summaries: list[str] = []
    last_output = ""
    interventions = 0

    for step in range(steps):
        history_summary = "; ".join(history_summaries[-3:])

        # Run primary agent and watchdog concurrently
        primary_task = asyncio.create_task(
            primary_agent_step(messages)
        )
        await asyncio.sleep(0)  # yield to allow task creation

        output = await primary_task
        last_output = output

        verdict = await watchdog_check(goal, output, history_summary, step)
        print(f"[Watchdog] Step {step}: on_track={verdict.on_track}, confidence={verdict.confidence}")

        messages.append({"role": "assistant", "content": output})
        history_summaries.append(output[:100])

        if not verdict.on_track and verdict.corrective_instruction:
            print(f"[Intervention] {verdict.corrective_instruction}")
            messages.append({
                "role": "user",
                "content": f"Correction needed: {verdict.corrective_instruction}"
            })
            interventions += 1
        elif step < steps - 1:
            messages.append({"role": "user", "content": "Continue."})

    print(f"Total watchdog interventions: {interventions}")
    return last_output

async def main():
    result = await run_with_watchdog(
        "Explain the differences between SQL and NoSQL databases",
        steps=3
    )
    print(result[:300])

asyncio.run(main())
```

## Solution 6: Drift Dashboard with Metrics Export

Log drift metrics to a structured format for monitoring dashboards.

```python
import asyncio
import json
import time
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass, asdict, field
from pathlib import Path

client = AsyncAnthropic()
METRICS_FILE = Path("/tmp/agent_drift_metrics.jsonl")

@dataclass
class DriftMetricRecord:
    session_id: str
    step: int
    timestamp: float
    goal_keyword_overlap: float
    output_length: int
    alert_level: str  # "none" | "warn" | "critical"
    output_snippet: str

def keyword_overlap_score(goal: str, output: str) -> float:
    goal_words = set(re.findall(r"\b\w{4,}\b", goal.lower()))
    output_words = set(re.findall(r"\b\w{4,}\b", output.lower()))
    if not goal_words:
        return 1.0
    return len(goal_words & output_words) / len(goal_words)

def export_metric(record: DriftMetricRecord):
    with METRICS_FILE.open("a") as f:
        f.write(json.dumps(asdict(record)) + "\n")

def load_metrics(session_id: str) -> list[DriftMetricRecord]:
    if not METRICS_FILE.exists():
        return []
    records = []
    for line in METRICS_FILE.read_text().splitlines():
        try:
            data = json.loads(line)
            if data.get("session_id") == session_id:
                records.append(DriftMetricRecord(**data))
        except Exception:
            continue
    return records

def print_drift_summary(session_id: str):
    records = load_metrics(session_id)
    if not records:
        print("No metrics found.")
        return
    print(f"\n=== Drift Summary: {session_id} ===")
    print(f"{'Step':<6} {'Overlap':<10} {'Length':<10} {'Alert':<10}")
    for r in records:
        print(f"{r.step:<6} {r.goal_keyword_overlap:<10.3f} {r.output_length:<10} {r.alert_level:<10}")
    avg = sum(r.goal_keyword_overlap for r in records) / len(records)
    alerts = sum(1 for r in records if r.alert_level != "none")
    print(f"\nAvg overlap: {avg:.3f} | Alerts: {alerts}/{len(records)}")

async def run_with_drift_dashboard(
    session_id: str,
    goal: str,
    steps: int = 5
) -> str:
    messages = [{"role": "user", "content": goal}]
    last_output = ""

    for step in range(steps):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=messages
        )
        output = response.content[0].text if response.content else ""
        last_output = output

        overlap = keyword_overlap_score(goal, output)
        alert = "none"
        if overlap < 0.1:
            alert = "critical"
        elif overlap < 0.2:
            alert = "warn"

        record = DriftMetricRecord(
            session_id=session_id,
            step=step,
            timestamp=time.time(),
            goal_keyword_overlap=round(overlap, 4),
            output_length=len(output),
            alert_level=alert,
            output_snippet=output[:100]
        )
        export_metric(record)

        if alert == "critical":
            print(f"[CRITICAL DRIFT] Step {step}: overlap={overlap:.3f}")
            messages.append({"role": "assistant", "content": output})
            messages.append({
                "role": "user",
                "content": f"Please return to the original goal: {goal}"
            })
        elif alert == "warn":
            print(f"[WARN DRIFT] Step {step}: overlap={overlap:.3f}")
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": "Stay focused on the original goal."})
        else:
            messages.append({"role": "assistant", "content": output})
            if step < steps - 1:
                messages.append({"role": "user", "content": "Continue."})

    print_drift_summary(session_id)
    return last_output

async def main():
    result = await run_with_drift_dashboard(
        session_id="session-001",
        goal="Summarize the key principles of agile software development",
        steps=4
    )
    print(f"\nFinal output:\n{result[:300]}")

asyncio.run(main())
```

## Comparison

| Approach | Accuracy | Latency Added | Storage | Best For |
|---|---|---|---|---|
| Embedding Cosine Monitor | High | Medium (embedding call) | None | Long pipelines with numeric thresholds |
| LLM Alignment Checker | Very High | High (extra LLM call) | None | Complex goals needing reasoning |
| Sliding Window Detector | Medium | Low | None | Gradual drift in conversations |
| Goal State Tracker | High | Medium | None | Structured multi-part goals |
| Dual-Agent Watchdog | Very High | High | None | High-stakes autonomous agents |
| Drift Dashboard | Medium | Low | Disk/JSONL | Observability, post-hoc analysis |

**Rule of thumb**: Use keyword overlap for cheap continuous monitoring, add an LLM alignment check at key milestones (every 3-5 steps), and log all metrics to a JSONL file for post-hoc analysis. Critical production agents benefit from the dual-agent watchdog pattern.
