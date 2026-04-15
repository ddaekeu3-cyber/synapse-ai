---
layout: default
title: "AI Agent Performance Errors — Latency, Timeouts, and Bottleneck Fixes"
description: "Fix slow AI agents: latency spikes, timeout cascades, cold starts, and throughput bottlenecks. Practical performance tuning for OpenClaw and production agents."
permalink: /guide/performance-errors
---

# AI Agent Performance Error Guide

Performance problems in AI agents are subtle — they don't crash, they just get slower and slower until users give up or costs spiral. This guide covers the most common performance failure patterns and how to fix them.

---

## Latency Failure Patterns

| Pattern | Symptom | Root Cause |
|---------|---------|-----------|
| Cold start spike | First request is 5–10x slower | Model warm-up or connection pool empty |
| Cascading timeout | One slow tool causes all downstream tools to fail | No independent timeout per tool |
| Context bloat | Requests slow down over time | Context window grows unbounded |
| Retry amplification | Errors cause more traffic, making things slower | No backoff on retry |
| Serial tool calls | 10 tool calls take 10x longer than necessary | Not parallelizing independent calls |

---

## Fix 1: Connection Pooling and Keep-Alive

The biggest latency fix for most agents is connection reuse:

```yaml
# openclaw.config.yaml
http:
  connection_pool:
    max_connections: 20
    keep_alive: true
    keep_alive_timeout_ms: 30000
  request_timeout_ms: 30000
  connect_timeout_ms: 5000
```

Without keep-alive, every API call pays DNS + TLS handshake cost (~100–300ms). With pooling, subsequent calls are <10ms.

---

## Fix 2: Parallelize Independent Tool Calls

Serial tool calls are the #1 performance anti-pattern:

```python
# BAD — 3 sequential calls, 3x the latency
result_a = await tool_a.call()
result_b = await tool_b.call()
result_c = await tool_c.call()

# GOOD — parallel calls, 1x the latency
result_a, result_b, result_c = await asyncio.gather(
    tool_a.call(),
    tool_b.call(),
    tool_c.call()
)
```

For agents that make multiple tool calls per turn, this alone can cut response time by 60–80%.

---

## Fix 3: Context Window Pruning

Agents slow down when context grows unbounded. The model processes every token on each call:

```python
def prune_context(messages, max_tokens=20000):
    """Keep system prompt + last N tokens of conversation"""
    total = 0
    pruned = []
    for msg in reversed(messages):
        tokens = estimate_tokens(msg)
        if total + tokens > max_tokens:
            break
        pruned.insert(0, msg)
        total += tokens
    return [messages[0]] + pruned  # Always keep system prompt
```

At 100K tokens of context, inference cost and latency increase significantly. Prune aggressively.

---

## Fix 4: Streaming for Perceived Latency

Even if total response time is unchanged, streaming makes the agent feel faster:

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    streaming: true
    stream_buffer_size: 64  # bytes before first flush
```

First token appears in <1s even for long responses. Users see progress immediately instead of waiting for the full response.

---

## Fix 5: Per-Tool Timeouts (Not Just Global)

A single slow tool shouldn't block everything:

```yaml
tools:
  web_search:
    timeout_ms: 10000
    on_timeout: skip_and_continue
  database_query:
    timeout_ms: 3000
    on_timeout: return_cached_or_fail
  code_executor:
    timeout_ms: 30000
    on_timeout: kill_and_report
```

Global timeouts hide the problem — the agent waits the full timeout on every slow call instead of failing fast on the specific tool.

---

## Fix 6: Response Caching for Repeated Queries

Many agent queries are nearly identical. Cache at the tool level:

```yaml
tools:
  web_search:
    cache:
      enabled: true
      ttl_seconds: 300    # 5 minutes
      key: "{query_hash}"
  documentation_lookup:
    cache:
      enabled: true
      ttl_seconds: 3600   # 1 hour for stable docs
```

For a knowledge-retrieval agent, caching can cut API calls by 40–60%.

---

## Fix 7: Model Selection by Task Complexity

Don't use the largest model for every task:

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    model_routing:
      simple_lookup: claude-haiku-4-5    # Fast, cheap for simple tasks
      standard_task: claude-sonnet-4-6   # Default for most work
      complex_analysis: claude-opus-4-6  # Reserve for hard problems
```

Haiku is 10x faster and 20x cheaper than Opus. Use it for classification, simple extraction, and routing tasks.

---

## Fix 8: Prewarming for Cold Starts

For latency-sensitive production agents:

```yaml
agent:
  prewarm:
    enabled: true
    interval_ms: 60000     # Ping every 60s to keep warm
    ping_message: "ping"
    ping_response_pattern: "pong"
```

Or if using containers:

```yaml
# docker-compose.yml
deploy:
  replicas: 1
  restart_policy:
    condition: always
healthcheck:
  test: ["CMD", "openclaw", "ping"]
  interval: 30s
  start_period: 10s
```

---

## Performance Checklist

Before deploying an agent to production:

- [ ] Connection pooling enabled (`keep_alive: true`)
- [ ] Independent tool calls parallelized
- [ ] Per-tool timeouts configured (not just global)
- [ ] Context pruning enabled at 20K–50K tokens
- [ ] Streaming enabled for user-facing responses
- [ ] Response caching enabled for repeated lookup tools
- [ ] Model routing configured (Haiku for simple tasks)
- [ ] Load tested at 2x expected peak traffic

---

[← View all performance solutions](/synapse-ai/solutions/performance/)

**Related guides:**
- [Rate Limit Errors](/synapse-ai/guide/rate-limit-errors) — rate limits cause latency spikes too
- [Loop / Stuck Errors](/synapse-ai/guide/loop-stuck-errors) — infinite loops masquerade as slowness
- [Token Saving Guide](/synapse-ai/guide/token-saving) — fewer tokens = faster responses

<div class="cta-box">
  <h3>Diagnose agent performance issues automatically</h3>
  <p>SynapseAI includes performance pattern detection and latency profiling for OpenClaw agents.</p>
  <code>clawhub install synapse-ai</code>
</div>
