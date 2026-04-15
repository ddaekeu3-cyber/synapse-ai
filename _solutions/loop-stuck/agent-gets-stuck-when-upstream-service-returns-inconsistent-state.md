---
layout: solution
title: "Agent Gets Stuck When Upstream Service Returns Inconsistent State"
category: loop-stuck
description: "Agent repeatedly polls a service that returns different values on successive calls — order status flips between 'pending' and 'shipped', flag alternates between true/false — causing the agent to loop indefinitely waiting for a stable state that never arrives."
tags: [loop-stuck, reliability, polling, consistency, flapping]
---

## Symptom

An agent polls an order service waiting for status to reach `"shipped"`:

```
[10:00:01] poll_order(id=42) → "pending"
[10:00:06] poll_order(id=42) → "shipped"   ← looks done!
[10:00:11] poll_order(id=42) → "pending"   ← reverted?
[10:00:16] poll_order(id=42) → "shipped"
[10:00:21] poll_order(id=42) → "pending"
... (agent loops forever waiting for stable "shipped")
```

The service is experiencing eventual consistency lag or a caching bug. The agent has no concept of "close enough" or "observed N times consistently".

## Root Cause

The agent's exit condition requires a single successful observation, but it retries on contradiction rather than accepting instability:

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: poll until single stable observation
def wait_for_status(order_id: int, target: str) -> str:
    while True:
        status = poll_order(order_id)
        if status == target:
            return status  # ← One observation is not enough
        time.sleep(5)
```

When the service flaps, each observation contradicts the last, and the loop never exits.

---

## Fix

### Option 1 — Quorum-based stability check: require N consistent observations

Only accept a state as stable after seeing it N times in a row. Single-observation flaps are ignored.

```python
import anthropic
import time
import json

client = anthropic.Anthropic(api_key="sk-live-...")

REQUIRED_CONFIRMATIONS = 3   # Must see target state 3 times in a row
POLL_INTERVAL = 2.0          # Seconds between polls
MAX_POLLS = 60               # Give up after 60 polls (~2 minutes)


def poll_order(order_id: int) -> str:
    """Simulated flapping service."""
    import random
    # 30% chance of returning wrong value (flapping)
    return "shipped" if random.random() > 0.3 else "pending"


def wait_for_stable_status(order_id: int, target: str) -> dict:
    """
    Wait for target status to appear REQUIRED_CONFIRMATIONS times consecutively.
    Returns result dict with stability metadata.
    """
    consecutive = 0
    history = []

    for poll_num in range(MAX_POLLS):
        status = poll_order(order_id)
        history.append(status)

        if status == target:
            consecutive += 1
            print(f"[quorum] Poll {poll_num + 1}: '{status}' ({consecutive}/{REQUIRED_CONFIRMATIONS})")
            if consecutive >= REQUIRED_CONFIRMATIONS:
                return {
                    "stable": True,
                    "status": status,
                    "confirmed_after": poll_num + 1,
                    "history_tail": history[-10:]
                }
        else:
            if consecutive > 0:
                print(f"[quorum] Flap detected after {consecutive} consecutive '{target}' — reset")
            consecutive = 0
            print(f"[quorum] Poll {poll_num + 1}: '{status}' (not target)")

        time.sleep(POLL_INTERVAL)

    return {
        "stable": False,
        "status": history[-1] if history else "unknown",
        "error": f"Did not reach stable '{target}' after {MAX_POLLS} polls",
        "history_tail": history[-10:]
    }


result = wait_for_stable_status(42, "shipped")
print(result)

# Expected Token Savings: quorum check exits on genuine state arrival; eliminates infinite flap loop
# Environment: agents polling distributed systems, eventual consistency stores, payment/order APIs
```

---

### Option 2 — Majority vote over a sliding window

Treat the most recently observed N values as a window. Accept the state that appears in the majority of the window.

```python
import anthropic
import time
import json
import random
from collections import Counter

client = anthropic.Anthropic(api_key="sk-live-...")

WINDOW_SIZE = 5          # Consider last 5 observations
MAJORITY_THRESHOLD = 3   # Majority of 5
POLL_INTERVAL = 1.5
MAX_POLLS = 50


def poll_service(resource_id: str) -> str:
    """Simulated flapping service — returns wrong value 25% of the time."""
    correct = "active" if resource_id != "broken" else "inactive"
    return correct if random.random() > 0.25 else ("inactive" if correct == "active" else "active")


def wait_for_majority_state(resource_id: str, target: str) -> dict:
    """
    Use majority voting over a sliding window to determine stable state.
    Resistant to single-observation flaps.
    """
    window: list[str] = []

    for poll_num in range(MAX_POLLS):
        observation = poll_service(resource_id)
        window.append(observation)

        # Keep only last WINDOW_SIZE observations
        if len(window) > WINDOW_SIZE:
            window.pop(0)

        counts = Counter(window)
        dominant, dominant_count = counts.most_common(1)[0]

        print(f"[majority] Poll {poll_num + 1}: '{observation}' | Window: {dict(counts)}")

        if dominant == target and dominant_count >= MAJORITY_THRESHOLD and len(window) == WINDOW_SIZE:
            return {
                "stable": True,
                "consensus_state": dominant,
                "confidence": dominant_count / WINDOW_SIZE,
                "polls": poll_num + 1,
                "window": window
            }

        time.sleep(POLL_INTERVAL)

    counts = Counter(window)
    return {
        "stable": False,
        "best_guess": counts.most_common(1)[0][0] if counts else "unknown",
        "window": window,
        "error": "Could not establish majority consensus within poll limit"
    }


result = wait_for_majority_state("server_01", "active")
print(result)

# Expected Token Savings: majority vote converges on real state despite flapping; avoids infinite loop
# Environment: health-check agents; infrastructure readiness polling; distributed cache consistency
```

---

### Option 3 — Agent asks Claude to interpret inconsistent state history

Pass the observation history to Claude and ask it to determine the most likely true state, treating the upstream inconsistency as a data problem.

```python
import anthropic
import time
import json
import random

client = anthropic.Anthropic(api_key="sk-live-...")


def poll_payment(payment_id: str) -> dict:
    """Flapping payment service — returns inconsistent states."""
    base = {"payment_id": payment_id, "confirmed": True, "amount": 99.99}
    if random.random() < 0.4:
        base["confirmed"] = False  # Flap
        base["status"] = "pending"
    else:
        base["status"] = "completed"
    return base


def gather_observations(payment_id: str, n: int = 5, interval: float = 0.5) -> list[dict]:
    """Collect N observations with timestamps."""
    observations = []
    for i in range(n):
        obs = poll_payment(payment_id)
        obs["observation_num"] = i + 1
        observations.append(obs)
        if i < n - 1:
            time.sleep(interval)
    return observations


def llm_interpret_inconsistency(payment_id: str, observations: list[dict]) -> dict:
    """Ask Claude to reason about conflicting observations and determine truth."""
    obs_text = json.dumps(observations, indent=2)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="""You are a data reliability expert analysing inconsistent API responses.
Given multiple observations of the same resource, determine:
1. The most likely true current state
2. Whether the inconsistency suggests a service bug or expected eventual consistency
3. A confidence level (0.0-1.0) in your assessment
4. Recommended action (accept_state, wait_longer, escalate, treat_as_error)

Return JSON:
{
  "likely_state": "...",
  "confidence": 0.X,
  "pattern": "flapping|eventual_consistency|transient_error",
  "recommendation": "accept_state|wait_longer|escalate|treat_as_error",
  "reasoning": "brief explanation"
}""",
        messages=[{
            "role": "user",
            "content": f"Resource: {payment_id}\n\nObservations:\n{obs_text}"
        }]
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        assessment = json.loads(raw.strip())
        return assessment
    except json.JSONDecodeError:
        return {
            "likely_state": "unknown",
            "confidence": 0.0,
            "recommendation": "escalate",
            "reasoning": "Failed to parse LLM assessment"
        }


observations = gather_observations("pay_789", n=6, interval=0.1)
assessment = llm_interpret_inconsistency("pay_789", observations)
print(json.dumps(assessment, indent=2))

# Expected Token Savings: LLM interprets ambiguous state → agent acts instead of looping
# Environment: agents handling payment, shipping, or other state-critical workflows with flaky backends
```

---

### Option 4 — Exponential backoff with jitter + hard max-wait cap

Add exponential backoff between polls so the agent doesn't hammer a struggling service, and enforce an absolute timeout that triggers a fallback path.

```python
import anthropic
import asyncio
import time
import random
import json

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

INITIAL_INTERVAL = 1.0     # Start at 1s
MAX_INTERVAL = 30.0        # Cap at 30s
BACKOFF_FACTOR = 2.0       # Double each time
JITTER_FACTOR = 0.3        # ±30% random jitter
ABSOLUTE_TIMEOUT = 120.0   # Give up after 2 minutes


async def poll_status_async(job_id: str) -> str:
    """Flapping async poll."""
    await asyncio.sleep(0.05)
    return "complete" if random.random() > 0.45 else "running"


async def poll_with_backoff(job_id: str, target: str) -> dict:
    """Poll with exponential backoff, jitter, and hard timeout."""
    interval = INITIAL_INTERVAL
    start_time = time.monotonic()
    attempt = 0

    last_seen: list[str] = []

    while time.monotonic() - start_time < ABSOLUTE_TIMEOUT:
        attempt += 1
        status = await poll_status_async(job_id)
        last_seen.append(status)
        elapsed = time.monotonic() - start_time

        print(f"[backoff] Attempt {attempt} ({elapsed:.1f}s): '{status}' (next wait: {interval:.1f}s)")

        # Check majority of last 3 observations to avoid flap exit
        if len(last_seen) >= 3:
            recent = last_seen[-3:]
            if recent.count(target) >= 2:  # At least 2 of 3 match target
                return {
                    "success": True,
                    "status": target,
                    "attempts": attempt,
                    "elapsed_seconds": elapsed,
                    "final_window": recent
                }

        # Compute next interval with jitter
        jitter = interval * JITTER_FACTOR * (2 * random.random() - 1)
        sleep_time = min(MAX_INTERVAL, interval + jitter)
        await asyncio.sleep(sleep_time)
        interval = min(MAX_INTERVAL, interval * BACKOFF_FACTOR)

    return {
        "success": False,
        "error": f"Timed out after {ABSOLUTE_TIMEOUT}s",
        "attempts": attempt,
        "last_seen": last_seen[-5:]
    }


result = asyncio.run(poll_with_backoff("job_abc", "complete"))
print(result)

# Expected Token Savings: backoff reduces wasted polls; hard timeout prevents infinite sessions
# Environment: async agents polling long-running jobs (ML training, video processing, batch exports)
```

---

### Option 5 — State machine with flap detection: flag inconsistent service, escalate

Model the polling loop as a state machine. Detect flapping as an explicit state and route it to an escalation handler rather than continuing to loop.

```python
import anthropic
import time
import json
import random
from enum import StrEnum

client = anthropic.Anthropic(api_key="sk-live-...")


class PollerState(StrEnum):
    POLLING = "polling"
    STABILIZING = "stabilizing"   # Seen target, confirming
    FLAPPING = "flapping"         # Oscillating between states
    STABLE = "stable"             # Confirmed stable
    TIMED_OUT = "timed_out"
    ESCALATED = "escalated"


FLAP_THRESHOLD = 3     # Declare flapping after 3 direction changes
MAX_POLLS = 40
CONFIRM_NEEDED = 2     # Need 2 consecutive to confirm stable


def observe(resource_id: str) -> str:
    return "healthy" if random.random() > 0.4 else "unhealthy"


def escalate(resource_id: str, history: list[str]) -> dict:
    """Escalation handler — called when service is definitively flapping."""
    print(f"[escalate] Service '{resource_id}' is flapping. History tail: {history[-8:]}")
    return {
        "action": "pagerduty_alert_sent",
        "resource": resource_id,
        "pattern": "flapping",
        "recommendation": "Check upstream service health and cache layer"
    }


def poll_with_flap_detection(resource_id: str, target: str) -> dict:
    state = PollerState.POLLING
    history: list[str] = []
    direction_changes = 0
    consecutive = 0
    last_value = None

    for poll_num in range(MAX_POLLS):
        obs = observe(resource_id)
        history.append(obs)

        # Track direction changes
        if last_value is not None and obs != last_value:
            direction_changes += 1
        last_value = obs

        # Flap detection
        if direction_changes >= FLAP_THRESHOLD:
            state = PollerState.FLAPPING
            print(f"[state-machine] FLAPPING detected after {direction_changes} changes")
            result = escalate(resource_id, history)
            return {"state": PollerState.ESCALATED, "escalation": result, "polls": poll_num + 1}

        # Stability logic
        if obs == target:
            consecutive += 1
            state = PollerState.STABILIZING
            print(f"[state-machine] Poll {poll_num + 1}: '{obs}' → STABILIZING ({consecutive}/{CONFIRM_NEEDED})")
            if consecutive >= CONFIRM_NEEDED:
                state = PollerState.STABLE
                return {"state": PollerState.STABLE, "status": target, "polls": poll_num + 1}
        else:
            consecutive = 0
            state = PollerState.POLLING
            print(f"[state-machine] Poll {poll_num + 1}: '{obs}' → POLLING (changes={direction_changes})")

        time.sleep(0.5)

    return {"state": PollerState.TIMED_OUT, "last": history[-1] if history else "unknown"}


result = poll_with_flap_detection("db_replica", "healthy")
print(json.dumps(result, indent=2))

# Expected Token Savings: flap detection terminates loops quickly; escalation prevents wasted polling
# Environment: infrastructure monitoring agents; SRE bots; service readiness checks
```

---

### Option 6 — Accept-on-first-positive with contradiction guard

Exit immediately on first positive observation, but only after verifying the observation isn't an isolated anomaly. If the next poll contradicts it, declare "unstable" and use the last-positive value.

```python
import anthropic
import time
import json
import random

client = anthropic.Anthropic(api_key="sk-live-...")


def quick_poll(task_id: str) -> dict:
    """Fast-path poll — flapping 35% of the time."""
    done = random.random() > 0.35
    return {
        "task_id": task_id,
        "status": "done" if done else "pending",
        "progress_pct": 100 if done else random.randint(60, 95)
    }


def accept_first_positive_with_guard(task_id: str, target_status: str) -> dict:
    """
    Accept first positive observation after a single confirmation check.
    If confirmation contradicts, report as 'likely complete' with lower confidence.
    """
    first_positive = None
    polls = 0

    while polls < 30:
        obs = quick_poll(task_id)
        polls += 1
        print(f"[fast-exit] Poll {polls}: {obs['status']} ({obs['progress_pct']}%)")

        if obs["status"] == target_status:
            first_positive = obs
            # Immediately verify with one more poll
            time.sleep(0.5)
            confirm = quick_poll(task_id)
            polls += 1

            if confirm["status"] == target_status:
                # Confirmed — exit
                return {
                    "result": "confirmed",
                    "status": target_status,
                    "confidence": 1.0,
                    "polls": polls
                }
            else:
                # Contradiction — service is flapping
                print(f"[fast-exit] Contradiction: {target_status} then {confirm['status']}")
                # Accept the positive as "likely true" with reduced confidence
                return {
                    "result": "accepted_with_caveat",
                    "status": target_status,
                    "confidence": 0.7,
                    "note": "Service returned inconsistent state; accepting first positive",
                    "polls": polls
                }

        time.sleep(1.0)

    return {"result": "timed_out", "last_status": obs["status"], "polls": polls}


result = accept_first_positive_with_guard("task_555", "done")
print(json.dumps(result, indent=2))

# Expected Token Savings: fast-path exits early on true completion; contradiction handled in 1 extra poll
# Environment: progress-tracking agents; job completion polling where speed matters
```

---

## Comparison

| Option | Loop-Breaking Strategy | Flap Tolerance | Max Polls | Complexity |
|--------|----------------------|----------------|-----------|------------|
| 1 | N consecutive confirmations | High | Configurable | Low |
| 2 | Majority vote window | High | Configurable | Low |
| 3 | LLM interpretation | Very high | Once | Medium |
| 4 | Exponential backoff + hard timeout | Medium | Time-bounded | Medium |
| 5 | Flap state machine + escalation | Explicit escalation | Configurable | Medium |
| 6 | Fast-exit + single contradiction guard | Medium | Configurable | Low |

**Recommended starting point:** Option 1 (N consecutive confirmations) — require 2-3 consecutive matching observations before accepting any state change. This is a one-line addition to any polling loop and eliminates single-observation flap acceptance. Add Option 5's direction-change counter as a flap detector with a configurable escalation threshold for production use.
