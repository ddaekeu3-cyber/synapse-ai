---
layout: default
title: "AI Agent Infinite Loop and Stuck Errors — Detection and Fixes"
description: "Fix infinite retry loops, stuck agents, and execution storms in OpenClaw. Covers circuit breakers, loop detection, token budget limits, and recovery patterns."
permalink: /guide/loop-stuck-errors
---

# AI Agent Loop and Stuck Error Guide

An agent stuck in an infinite loop is the most expensive failure mode in AI systems. Unlike a crashed agent that stops billing, a looping agent keeps generating tokens — often burning $10–50 before a human notices.

---

## Why Agents Get Stuck

Agents loop for three main reasons:

1. **Retry without backoff:** Tool call fails → agent retries immediately → fails again → retries → infinite loop
2. **Undetected task completion:** Agent completes the task but doesn't recognize its own success condition
3. **Dependency deadlock:** Agent waits for output from a subtask that is itself waiting

All three are preventable with the right configuration.

---

## The Token Cost of a Stuck Agent

| Loop Duration | Typical Token Burn | Cost at Standard Rates |
|--------------|-------------------|----------------------|
| 5 minutes | 15,000–30,000 | $4.50–9.00 |
| 15 minutes | 50,000–100,000 | $15–30 |
| 1 hour | 200,000–400,000 | $60–120 |

Most stuck agents go unnoticed for 10–30 minutes. That's $15–90 per incident.

---

## Fix 1: Circuit Breaker (Required)

The single most effective fix. A circuit breaker stops retrying after N failures:

```yaml
# openclaw.config.yaml
agent:
  circuit_breaker:
    failure_threshold: 3        # Open after 3 consecutive failures
    reset_timeout_ms: 30000     # Try again after 30 seconds
    half_open_attempts: 1       # Test with 1 request before fully reopening
    on_open: log_and_notify     # What to do when circuit opens
```

With a circuit breaker, a stuck tool call fails fast instead of looping forever.

---

## Fix 2: Per-Session Token Budget

Cap total tokens per session to bound worst-case cost:

```yaml
limits:
  max_tokens_per_session: 50000
  max_tokens_per_task: 10000
  on_limit_reached: pause_and_report  # Not: silently_fail
```

When the limit is reached, the agent pauses, reports its current state, and waits for human input — rather than being silently killed.

---

## Fix 3: Loop Detection

Detect when the agent is repeating the same actions:

```yaml
agent:
  loop_detection:
    enabled: true
    window_size: 10           # Check last 10 actions
    similarity_threshold: 0.85 # Flag if 85% similar to a previous action
    on_detected: break_and_report
```

This catches the "try same thing 10 times" pattern before it burns significant tokens.

---

## Fix 4: Explicit Success Conditions

Agents loop when they don't know they're done. Define explicit success conditions:

```yaml
task:
  success_conditions:
    - type: file_exists
      path: ./output/result.json
    - type: api_response
      status: 200
      endpoint: /health
  failure_conditions:
    - type: max_attempts
      count: 5
    - type: elapsed_time_ms
      max: 60000
```

Without explicit conditions, the agent uses the model's judgment — which can loop on "not quite right" indefinitely.

---

## Fix 5: Heartbeat Monitoring

For long-running agents, monitor liveness and progress — not just "is the process alive":

```bash
# Check if agent is making progress (not just running)
openclaw agent status --session $SESSION_ID --check-progress

# Output:
# last_action: 47s ago
# last_output_token: 12s ago
# loop_score: 0.23 (low = good, high = looping)
```

Add a watchdog that kills the session if `loop_score > 0.7` for more than 60 seconds.

---

## The Exec Storm Pattern (High Cost)

The most expensive stuck pattern — invisible on the output channel:

**What happens:**
1. Agent tool call fails with an error not surfaced to the output channel (e.g., Telegram)
2. Agent retries the tool call in an internal loop
3. No output to Telegram → user sees silence
4. Agent burns 3–5x normal token rate
5. After 10–30 minutes, either times out or OOMs the gateway

**Symptoms:**
- Telegram channel: no output
- Gateway CPU: elevated (30–80%)
- `openclaw logs --session $ID`: rapid tool call failures
- Token counter: growing faster than expected

**Fix:**
```yaml
error_handling:
  mirror_tool_errors_to_channels: [telegram, slack]
  circuit_breaker:
    failure_threshold: 3
    reset_timeout_ms: 30000
channels:
  telegram:
    on_agent_silent_for_ms: 120000  # Alert after 2 min of silence
      action: send_status_update
```

[Full exec storm solution →](/synapse-ai/solutions/general/agent-exec-storm-invisible-on-telegram)

---

## The Unresponsive-But-Running Pattern

**What happens:**
Agent process is alive. Health endpoint returns 200. But agent is not processing any new requests.

**Symptom:** `openclaw status` shows "running", channels show "connected", but messages go unanswered.

**Root cause:** Agent thread is blocked on a synchronous operation (database read, network call) with no timeout configured.

**Fix:**
```yaml
agent:
  operation_timeout_ms: 10000    # Timeout any single operation at 10s
  watchdog:
    enabled: true
    heartbeat_interval_ms: 5000
    max_missed_heartbeats: 3     # Restart after 3 missed (15s)
```

[Full solution →](/synapse-ai/solutions/telegram/telegram-agent-alive-but-unresponsive)

---

## Recovery After a Stuck Agent

When you discover a stuck agent:

```bash
# 1. Save what you can from current state
openclaw session export $SESSION_ID > session-backup.json

# 2. Kill the stuck session
openclaw session kill $SESSION_ID

# 3. Check what was actually completed
openclaw logs --session $SESSION_ID --filter "completed|success" --last 100

# 4. Start fresh from last known good state
openclaw session restore session-backup.json --from-checkpoint last_success
```

---

## Detection Checklist

Before deploying any autonomous agent:

- [ ] Circuit breaker configured with `failure_threshold ≤ 5`
- [ ] Per-session token budget set
- [ ] Loop detection enabled
- [ ] Explicit success and failure conditions defined
- [ ] Output channel error mirroring enabled
- [ ] Watchdog heartbeat monitoring active
- [ ] Human notification on circuit open or budget limit

---

[← View all loop/stuck solutions](/synapse-ai/solutions/loop-stuck/)

<div class="cta-box">
  <h3>Stop stuck agent token burns</h3>
  <p>SynapseAI detects loop patterns in real time and triggers circuit breakers before the token cost becomes significant.</p>
  <code>clawhub install synapse-ai</code>
</div>
