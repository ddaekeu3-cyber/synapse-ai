---
title: "Agent Doesn't Implement Adaptive Exit Strategy for Stuck Tasks"
description: "When an agent cannot make progress, it needs escalating exit strategies: simplify, reframe, decompose, escalate, or gracefully abort with a partial result rather than looping forever or crashing."
difficulty: intermediate
category: loop-stuck
tags: [stuck, exit-strategy, adaptive, timeout, graceful-abort, partial-results]
---

## Problem

Agents get stuck in many ways — unsatisfiable constraints, tool failures, ambiguous goals, resource exhaustion, or reasoning dead ends. Without an adaptive exit strategy, the agent either loops indefinitely, crashes with an unhelpful error, or returns a blank response. Users need the agent to try progressively softer strategies before giving up, and when it does give up, to return the best partial result it has.

```python
# BAD: no exit strategy — loops or crashes
async def run_agent(task: str) -> str:
    while True:
        result = await call_model(task)
        if is_complete(result):
            return result
        # infinite loop if task is unsolvable
```

## Solution 1: Escalating Strategy Ladder with Budget

Try progressively simpler approaches before aborting.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Callable, Any

client = AsyncAnthropic()

@dataclass
class ExitStrategy:
    name: str
    max_attempts: int
    transform: Callable[[str], str]  # modifies the task prompt

@dataclass
class ExitResult:
    success: bool
    strategy_used: str
    attempts: int
    output: str
    partial: bool = False

STRATEGIES = [
    ExitStrategy("direct", 3, lambda t: t),
    ExitStrategy("simplified", 2, lambda t: f"Simplified version: {t}\n\nFocus only on the core requirement."),
    ExitStrategy("step_by_step", 2, lambda t: f"Break this into steps: {t}\n\nList numbered steps, then execute step 1 only."),
    ExitStrategy("minimal", 1, lambda t: f"Minimal answer only (one sentence): {t}"),
    ExitStrategy("acknowledgment", 1, lambda t: f"Explain why this task is difficult and what partial progress you can offer: {t}"),
]

async def run_with_adaptive_exit(task: str, tools: list[dict] | None = None) -> ExitResult:
    last_output = ""

    for strategy in STRATEGIES:
        transformed = strategy.transform(task)
        for attempt in range(strategy.max_attempts):
            try:
                kwargs: dict[str, Any] = {
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": transformed}],
                }
                if tools:
                    kwargs["tools"] = tools

                response = await asyncio.wait_for(
                    client.messages.create(**kwargs),
                    timeout=30.0
                )
                output = response.content[0].text if response.content else ""
                last_output = output

                if response.stop_reason == "end_turn" and len(output) > 20:
                    is_partial = strategy.name != "direct"
                    return ExitResult(
                        success=True,
                        strategy_used=strategy.name,
                        attempts=attempt + 1,
                        output=output,
                        partial=is_partial
                    )

            except asyncio.TimeoutError:
                continue
            except Exception:
                continue

    return ExitResult(
        success=False,
        strategy_used="exhausted",
        attempts=sum(s.max_attempts for s in STRATEGIES),
        output=last_output or "Unable to complete task after all exit strategies.",
        partial=True
    )

async def main():
    result = await run_with_adaptive_exit(
        "Solve P=NP and provide a full formal proof"
    )
    print(f"Strategy: {result.strategy_used}, Partial: {result.partial}")
    print(result.output[:200])

asyncio.run(main())
```

## Solution 2: Stuck-State Detector with Automatic Reframing

Detect semantic stagnation and reframe the goal.

```python
import asyncio
import hashlib
from anthropic import AsyncAnthropic
from collections import deque

client = AsyncAnthropic()

class StuckDetector:
    def __init__(self, window: int = 3, similarity_threshold: float = 0.85):
        self.window = window
        self.threshold = similarity_threshold
        self.recent_hashes: deque[str] = deque(maxlen=window)
        self.recent_outputs: deque[str] = deque(maxlen=window)
        self.stuck_count = 0

    def _fingerprint(self, text: str) -> str:
        # simple n-gram fingerprint
        words = text.lower().split()
        bigrams = set(zip(words, words[1:]))
        key = " ".join(sorted(f"{a}_{b}" for a, b in bigrams)[:20])
        return hashlib.md5(key.encode()).hexdigest()

    def is_stuck(self, output: str) -> bool:
        fp = self._fingerprint(output)
        if fp in self.recent_hashes:
            self.stuck_count += 1
            return True
        self.recent_hashes.append(fp)
        self.recent_outputs.append(output)
        self.stuck_count = 0
        return False

    def get_reframe_prompt(self, original_task: str, stuck_outputs: list[str]) -> str:
        sample = stuck_outputs[-1][:300] if stuck_outputs else ""
        return (
            f"You keep producing similar responses to this task:\n\n"
            f"TASK: {original_task}\n\n"
            f"RECENT RESPONSE SAMPLE: {sample}\n\n"
            f"This approach isn't working. Try a completely different angle:\n"
            f"1. Question your assumptions\n"
            f"2. Use a different method or format\n"
            f"3. Provide a partial result with clear caveats\n\n"
            f"New attempt:"
        )

async def run_with_stuck_detection(task: str, max_rounds: int = 8) -> str:
    detector = StuckDetector()
    messages = [{"role": "user", "content": task}]
    best_output = ""
    reframes = 0
    max_reframes = 2

    for round_num in range(max_rounds):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=messages
        )
        output = response.content[0].text if response.content else ""

        if len(output) > len(best_output):
            best_output = output

        if response.stop_reason == "end_turn" and not detector.is_stuck(output):
            return output

        if detector.stuck_count >= 2 and reframes < max_reframes:
            reframes += 1
            reframe = detector.get_reframe_prompt(task, list(detector.recent_outputs))
            messages = [{"role": "user", "content": reframe}]
            print(f"[Round {round_num}] Stuck detected, reframing (attempt {reframes})")
        else:
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": "Continue or conclude."})

    return best_output or "Task could not be completed."

async def main():
    result = await run_with_stuck_detection("Explain the color blue to someone born blind")
    print(result[:300])

asyncio.run(main())
```

## Solution 3: Constraint Relaxation Exit

Progressively relax constraints until the task is solvable.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class Constraint:
    name: str
    description: str
    relaxed_description: str
    priority: int  # lower = relax first

def build_constrained_prompt(task: str, active_constraints: list[Constraint]) -> str:
    if not active_constraints:
        return f"Do your best: {task}"
    constraint_text = "\n".join(
        f"- {c.description}" for c in sorted(active_constraints, key=lambda x: x.priority)
    )
    return f"{task}\n\nConstraints:\n{constraint_text}"

async def run_with_constraint_relaxation(
    task: str,
    constraints: list[Constraint],
    max_relaxations: int = 3
) -> tuple[str, list[str]]:
    active = list(constraints)
    relaxed_names: list[str] = []

    for relaxation_round in range(max_relaxations + 1):
        prompt = build_constrained_prompt(task, active)
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}]
                ),
                timeout=25.0
            )
            output = response.content[0].text if response.content else ""

            # Check if model indicates it cannot proceed
            stuck_signals = ["cannot", "impossible", "unable to", "I can't", "no way to"]
            if not any(s in output.lower() for s in stuck_signals) and len(output) > 50:
                return output, relaxed_names

        except Exception:
            pass

        # Relax the lowest-priority constraint
        if active:
            to_relax = min(active, key=lambda c: c.priority)
            active = [c for c in active if c.name != to_relax.name]
            relaxed_names.append(to_relax.name)
            print(f"[Relaxation {relaxation_round+1}] Relaxing constraint: {to_relax.name}")

    # Final attempt with no constraints
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": f"Best effort, no constraints: {task}"}]
    )
    output = response.content[0].text if response.content else "Unable to complete."
    return output, relaxed_names

async def main():
    constraints = [
        Constraint("length", "Response must be under 10 words", "Response under 50 words", 1),
        Constraint("format", "Response must be valid JSON only", "Response can be prose", 2),
        Constraint("citation", "Every claim must cite a source", "Citations optional", 3),
    ]
    result, relaxed = await run_with_constraint_relaxation(
        "Explain quantum entanglement simply",
        constraints
    )
    print(f"Relaxed: {relaxed}")
    print(result[:200])

asyncio.run(main())
```

## Solution 4: Graceful Abort with Best Partial Result

Collect partial results throughout and return the best on timeout.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class PartialResult:
    content: str
    quality_score: float
    phase: str
    timestamp: float = field(default_factory=time.time)

class PartialResultCollector:
    def __init__(self):
        self.results: list[PartialResult] = []

    def add(self, content: str, phase: str):
        if not content.strip():
            return
        # Simple quality heuristic: length + completeness signals
        completeness = 1.0 if any(
            content.strip().endswith(c) for c in ".!?\n```"
        ) else 0.5
        score = min(len(content) / 500, 1.0) * completeness
        self.results.append(PartialResult(content, score, phase))

    def best(self) -> PartialResult | None:
        return max(self.results, key=lambda r: r.quality_score) if self.results else None

async def run_with_graceful_abort(
    task: str,
    phases: list[str],
    total_timeout: float = 60.0
) -> dict:
    collector = PartialResultCollector()
    deadline = time.time() + total_timeout
    completed_phases: list[str] = []

    for phase in phases:
        remaining = deadline - time.time()
        if remaining <= 2.0:
            print(f"[Abort] Deadline reached before phase: {phase}")
            break

        phase_prompt = f"Phase '{phase}' for task: {task}\n\nComplete this phase only."
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=[{"role": "user", "content": phase_prompt}]
                ),
                timeout=min(remaining - 1.0, 20.0)
            )
            output = response.content[0].text if response.content else ""
            collector.add(output, phase)
            completed_phases.append(phase)
            print(f"[Phase] Completed: {phase}")

        except asyncio.TimeoutError:
            print(f"[Timeout] Phase timed out: {phase}")
            break
        except Exception as e:
            print(f"[Error] Phase {phase} failed: {e}")

    best = collector.best()
    all_results = "\n\n---\n\n".join(
        f"## {r.phase}\n{r.content}" for r in collector.results
    )

    return {
        "completed_phases": completed_phases,
        "partial": len(completed_phases) < len(phases),
        "best_phase": best.phase if best else None,
        "combined_output": all_results,
        "best_output": best.content if best else "No output collected.",
    }

async def main():
    result = await run_with_graceful_abort(
        task="Design a microservices architecture for an e-commerce platform",
        phases=["requirements", "service_boundaries", "data_models", "api_contracts", "deployment"],
        total_timeout=45.0
    )
    print(f"Completed: {result['completed_phases']}")
    print(f"Partial: {result['partial']}")
    print(result["best_output"][:300])

asyncio.run(main())
```

## Solution 5: Meta-Reasoner Exit Arbitrator

A separate model call decides whether to continue, reframe, or abort.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

ARBITRATOR_PROMPT = """You are an exit strategy arbitrator. Given an agent's task and its recent attempts, decide the next action.

Respond with JSON only:
{
  "action": "continue" | "reframe" | "decompose" | "abort",
  "reason": "brief explanation",
  "modified_task": "new task if reframing, else null",
  "subtasks": ["list", "if", "decomposing"] or null
}

Actions:
- continue: agent is making progress, keep going
- reframe: agent is stuck, try a different angle (provide modified_task)
- decompose: task too large, break it up (provide subtasks list of 2-3 items)
- abort: task is fundamentally unsolvable or out of scope
"""

async def arbitrate(task: str, attempts: list[str]) -> dict:
    attempts_text = "\n\n".join(
        f"Attempt {i+1}:\n{a[:300]}" for i, a in enumerate(attempts[-3:])
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=ARBITRATOR_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Task: {task}\n\nRecent attempts:\n{attempts_text}"
        }]
    )
    text = response.content[0].text.strip()
    try:
        # Extract JSON from response
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"action": "abort", "reason": "arbitrator failed", "modified_task": None, "subtasks": None}

async def run_with_arbitration(task: str, max_rounds: int = 6) -> str:
    attempts: list[str] = []
    current_task = task

    for round_num in range(max_rounds):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": current_task}]
        )
        output = response.content[0].text if response.content else ""
        attempts.append(output)

        if response.stop_reason == "end_turn" and len(output) > 100:
            # Check with arbitrator every 2 rounds
            if round_num % 2 == 1:
                decision = await arbitrate(task, attempts)
                action = decision.get("action", "continue")
                print(f"[Round {round_num}] Arbitrator: {action} — {decision.get('reason','')}")

                if action == "abort":
                    return attempts[-1] or "Task aborted by arbitrator."
                elif action == "reframe" and decision.get("modified_task"):
                    current_task = decision["modified_task"]
                    attempts = []
                elif action == "decompose" and decision.get("subtasks"):
                    # Run subtasks and combine
                    subtask_results = await asyncio.gather(*[
                        client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=256,
                            messages=[{"role": "user", "content": st}]
                        )
                        for st in decision["subtasks"]
                    ])
                    combined = "\n\n".join(
                        r.content[0].text for r in subtask_results if r.content
                    )
                    return combined
                # else continue
            else:
                return output  # looks good, return early

    return attempts[-1] if attempts else "Could not complete task."

async def main():
    result = await run_with_arbitration("Translate the smell of rain into music notation")
    print(result[:300])

asyncio.run(main())
```

## Solution 6: Exponential Fallback with Context Preservation

Each failed attempt informs the next, preserving what worked.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class AttemptRecord:
    attempt_num: int
    strategy: str
    output: str
    tokens_used: int
    success: bool

FALLBACK_TEMPLATES = [
    ("full_task",       lambda t, _: t),
    ("guided",          lambda t, prev: f"{t}\n\nHint: Previous attempts produced: {prev[:100]}... Try a different angle."),
    ("minimal_scope",   lambda t, _: f"Answer only the most essential part of: {t}"),
    ("analogy",         lambda t, _: f"Use an analogy to explain: {t}"),
    ("bullet_summary",  lambda t, _: f"In 3 bullet points only: {t}"),
    ("honest_limits",   lambda t, _: f"What can you say about this, and what can't you say? Task: {t}"),
]

async def run_with_exponential_fallback(task: str) -> list[AttemptRecord]:
    records: list[AttemptRecord] = []
    prev_output = ""
    base_delay = 0.5

    for i, (strategy_name, template_fn) in enumerate(FALLBACK_TEMPLATES):
        prompt = template_fn(task, prev_output)
        delay = base_delay * (2 ** i)  # exponential: 0.5, 1, 2, 4, 8, 16s

        try:
            await asyncio.sleep(min(delay, 5.0))  # cap at 5s
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}]
                ),
                timeout=20.0
            )
            output = response.content[0].text if response.content else ""
            tokens = response.usage.output_tokens
            success = len(output) > 30 and response.stop_reason == "end_turn"
            prev_output = output

            record = AttemptRecord(i + 1, strategy_name, output, tokens, success)
            records.append(record)
            print(f"[Attempt {i+1}] {strategy_name}: {'✓' if success else '✗'} ({tokens} tokens)")

            if success:
                break

        except asyncio.TimeoutError:
            records.append(AttemptRecord(i + 1, strategy_name, "", 0, False))
            print(f"[Attempt {i+1}] {strategy_name}: timeout")
        except Exception as e:
            records.append(AttemptRecord(i + 1, strategy_name, str(e), 0, False))
            print(f"[Attempt {i+1}] {strategy_name}: error")

    return records

async def main():
    records = await run_with_exponential_fallback(
        "Prove that consciousness is purely computational"
    )
    successful = [r for r in records if r.success]
    if successful:
        best = successful[-1]
        print(f"\nSucceeded with strategy: {best.strategy}")
        print(best.output[:300])
    else:
        print("\nAll strategies exhausted.")
        last = records[-1] if records else None
        if last:
            print(f"Last attempt ({last.strategy}): {last.output[:200]}")

asyncio.run(main())
```

## Comparison

| Approach | Best For | Latency | Complexity | Partial Result Quality |
|---|---|---|---|---|
| Strategy Ladder | General stuck tasks | Low-Med | Low | Medium |
| Stuck-State Detector | Repetitive loops | Low | Medium | High |
| Constraint Relaxation | Over-constrained tasks | Medium | Medium | High |
| Graceful Abort | Long multi-phase tasks | Varies | Medium | High |
| Meta-Reasoner Arbitrator | Complex task routing | Medium | High | High |
| Exponential Fallback | Unpredictably failing tasks | Medium-High | Low | Medium |

**Rule of thumb**: Use the strategy ladder for simple agents, the stuck-state detector for multi-turn conversations, and graceful abort for long-running pipelines where partial output has value.
