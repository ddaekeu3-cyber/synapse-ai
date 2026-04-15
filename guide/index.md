---
layout: default
title: "AI Agent Error Guides — Troubleshooting Playbooks"
description: "Comprehensive guides for every category of AI agent error. Token saving, rate limits, auth, loops, hallucination, tool failures, concurrency, and more."
permalink: /guide/
---

# AI Agent Troubleshooting Guides

Step-by-step playbooks for every category of agent error. Each guide covers root causes, fix patterns, and checklists for production deployments.

---

<div class="category-grid">
  <a class="category-card" href="/synapse-ai/guide/token-saving">
    <h3>Token Saving</h3>
    <p>Reduce token waste, optimize context usage, cut costs without degrading output quality</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/openclaw-errors">
    <h3>OpenClaw Errors</h3>
    <p>Gateway failures, skill errors, session issues, channel connectivity problems</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/rate-limit-errors">
    <h3>Rate Limit Errors</h3>
    <p>429 errors, exponential backoff, queue-based throttling, model fallback strategies</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/auth-errors">
    <h3>Auth Errors</h3>
    <p>401/403 failures, OAuth refresh, JWT validation, credential caching, auth loops</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/context-window-errors">
    <h3>Context Window Errors</h3>
    <p>Overflow handling, summarization, truncation strategies, model context limits</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/loop-stuck-errors">
    <h3>Loop / Stuck Errors</h3>
    <p>Circuit breakers, retry storms, infinite loops, watchdog patterns, exec storms</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/docker-errors">
    <h3>Docker / Sandbox Errors</h3>
    <p>EACCES permission errors, networking, OOM, volume mounts, container isolation</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/memory-session-errors">
    <h3>Memory / Session Errors</h3>
    <p>Session amnesia, file persistence, cross-session handoff, context loss prevention</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/tool-failure-errors">
    <h3>Tool / MCP Failures</h3>
    <p>Schema mismatches, timeouts, plugin crashes, function call errors, empty results</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/hallucination-prevention">
    <h3>Hallucination Prevention</h3>
    <p>Verification pipelines, grounding, temperature calibration, uncertainty expression</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/performance-errors">
    <h3>Performance Errors</h3>
    <p>Latency spikes, cold starts, context bloat, connection pooling, tool parallelization</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/concurrency-errors">
    <h3>Concurrency Errors</h3>
    <p>Race conditions, deadlocks, session isolation, message deduplication, ordering</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/config-errors">
    <h3>Config Errors</h3>
    <p>Missing env vars, YAML syntax, model IDs, base URL format, secrets management</p>
  </a>
  <a class="category-card" href="/synapse-ai/guide/prompt-engineering">
    <h3>Prompt Engineering</h3>
    <p>Injection prevention, instruction failures, role confusion, output format control</p>
  </a>
</div>

---

## Quick Reference: Which Guide Do I Need?

| Symptom | Guide |
|---------|-------|
| Agent returns wrong output / makes things up | [Hallucination Prevention](/synapse-ai/guide/hallucination-prevention) |
| Agent loops forever or gets stuck | [Loop / Stuck Errors](/synapse-ai/guide/loop-stuck-errors) |
| 429 Too Many Requests errors | [Rate Limit Errors](/synapse-ai/guide/rate-limit-errors) |
| 401 / 403 errors, auth failures | [Auth Errors](/synapse-ai/guide/auth-errors) |
| Tool call fails or returns wrong data | [Tool / MCP Failures](/synapse-ai/guide/tool-failure-errors) |
| Agent forgets context after restart | [Memory / Session Errors](/synapse-ai/guide/memory-session-errors) |
| Context too long / overflow errors | [Context Window Errors](/synapse-ai/guide/context-window-errors) |
| Agent running slow / timing out | [Performance Errors](/synapse-ai/guide/performance-errors) |
| Costs too high / too many tokens used | [Token Saving](/synapse-ai/guide/token-saving) |
| Agent not following instructions | [Prompt Engineering](/synapse-ai/guide/prompt-engineering) |
| Docker / container crashes | [Docker / Sandbox Errors](/synapse-ai/guide/docker-errors) |
| Race conditions, duplicate messages | [Concurrency Errors](/synapse-ai/guide/concurrency-errors) |
| Agent won't start / config broken | [Config Errors](/synapse-ai/guide/config-errors) |
| OpenClaw gateway errors | [OpenClaw Errors](/synapse-ai/guide/openclaw-errors) |

---

[← Back to all solutions](/synapse-ai/)
