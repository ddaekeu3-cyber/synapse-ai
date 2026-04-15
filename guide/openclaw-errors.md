---
layout: default
title: "OpenClaw Error Reference 2026 — Complete Troubleshooting Guide"
description: "253+ documented OpenClaw errors with root causes and fixes. Gateway crashes, skill failures, auth issues, rate limits, session drops — solved."
permalink: /guide/openclaw-errors
---

# OpenClaw Error Reference 2026

A categorized reference of the most common OpenClaw errors, their root causes, and verified fixes. Each entry links to a full solution page with copy-paste steps.

---

## Gateway Errors

The OpenClaw gateway is the most failure-prone component because it bridges your agent to external APIs, channels, and tools simultaneously.

### Gateway crashes silently (no error in logs)

**Symptom:** Gateway process stops accepting connections but returns no error. `openclaw status` shows running; channels show disconnected.

**Root cause:** Memory pressure or an unhandled exception in a channel handler causes the gateway to enter a zombie state — alive but not processing.

**Fix:** Add health check polling and automatic restart on unresponsive gateway:

```bash
# Check if gateway is actually responsive
curl -s --max-time 5 http://localhost:9991/health || openclaw gateway restart
```

Add to cron or systemd watchdog. [Full solution →](/synapse-ai/solutions/openclaw/)

---

### Gateway WebSocket handshake timeout (silent 1000 close)

**Symptom:** CLI commands fail with silent close code 1000. Gateway logs show no error.

**Root cause:** The default CLI WebSocket handshake timeout is 3 seconds (`DEFAULT_HANDSHAKE_TIMEOUT_MS = 3e3`). On loaded gateways (600MB+ RAM, 80+ tasks), this is consistently too short.

**Fix:** Patch to 15s via a startup script that survives updates. [Full solution →](/synapse-ai/solutions/telegram/cli-handshake-timeout-3s-too-short)

---

## Auth & Credential Errors

### 401 on every request despite valid API key

**Symptom:** All API calls return 401 even after re-entering credentials. Key works in curl directly.

**Root cause:** OpenClaw caches auth headers at gateway startup. Rotating a key requires a gateway restart, not just a settings update.

**Fix:**
```bash
openclaw gateway restart
```

If still failing, check that the key in `~/.openclaw/credentials.json` matches what was just set. [Full solutions →](/synapse-ai/solutions/auth/)

---

### OAuth refresh token silently discarded

**Symptom:** Agent loses OAuth access after ~1 hour. Refresh token present but not used.

**Root cause:** Some OAuth providers return a new refresh token on each refresh. OpenClaw only stores the original. The stored refresh token becomes invalid after first use.

**Fix:** Enable token rotation in config:
```yaml
oauth:
  rotate_refresh_token: true
  persist_path: ~/.openclaw/oauth-tokens.json
```

[Full solution →](/synapse-ai/solutions/auth/)

---

## Rate Limit Errors

### Anthropic API 429 — too many requests

**Symptom:** `Error 429: rate_limit_error` from Anthropic API. Happens during peak usage or batch operations.

**Root cause:** Tier-based rate limits (RPM/TPM). Default OpenClaw config does not implement exponential backoff.

**Fix:** Add retry with exponential backoff:
```yaml
# openclaw.config.yaml
providers:
  anthropic:
    retry:
      max_attempts: 5
      initial_delay_ms: 2000
      backoff_multiplier: 2.0
      jitter: true
```

[Full solutions →](/synapse-ai/solutions/rate-limit/)

---

### Anthropic API 529 — overloaded, model unavailable

**Symptom:** `Error 529: overloaded_error`. Typically during peak hours (14:00–18:00 UTC).

**Root cause:** Provider-side capacity constraint. No client-side fix.

**Fix:** Add automatic model fallback:
```yaml
providers:
  anthropic:
    fallback_model: claude-sonnet-4-5
    on_errors: [529, 503]
```

Sonnet availability is higher during Opus overload periods. [Full solution →](/synapse-ai/solutions/openclaw/)

---

## Session & Context Errors

### Agent forgets context after gateway restart

**Symptom:** Agent behaves as if starting fresh after any restart. Previous decisions and context gone.

**Root cause:** Conversation state stored in memory, not persisted to disk. Gateway restart = full memory wipe.

**Fix:** Enable session persistence:
```yaml
memory:
  persist: true
  storage: ~/.openclaw/sessions/
  compress: true
```

Then explicitly save checkpoints at task boundaries. [Full solutions →](/synapse-ai/solutions/memory/)

---

### Context window overflow on long sessions

**Symptom:** `Error: context_length_exceeded` or model truncates early responses. Happens after 20–30 message exchanges.

**Root cause:** Full conversation history sent with every request. History grows linearly; model context window is fixed.

**Fix options:**
1. Enable automatic summarization at 80% context usage
2. Use `.clawignore` to exclude large files from context
3. Switch to a model with larger context (claude-3-7-sonnet: 200k tokens)

[Full solutions →](/synapse-ai/solutions/context-window/)

---

## Channel-Specific Errors

### Telegram channel stops responding (polling stall)

**Symptom:** Telegram messages not received. No errors in logs. `openclaw status` shows Telegram channel "initializing" indefinitely.

**Root cause:** Long-polling connection stalled. Gateway does not detect the stall and does not retry.

**Fix:** Downgrade to last stable version or patch polling with timeout detection:
```bash
npm install -g openclaw@2026.3.22
openclaw gateway restart
```

[Full solutions →](/synapse-ai/solutions/telegram/)

---

## Loop & Stuck Agent Errors

### Agent retry storm invisible on Telegram

**Symptom:** Agent appears stuck. No output on Telegram. CPU usage high on gateway host. Token burn rate 3–5x normal.

**Root cause:** Agent is retrying a failed tool call in a tight loop. Tool call errors are not surfaced to the Telegram channel, only to internal logs.

**Fix:** Enable cross-channel error mirroring and add circuit breaker:
```yaml
error_handling:
  mirror_to_channels: [telegram]
  circuit_breaker:
    failure_threshold: 3
    reset_timeout_ms: 30000
```

[Full solution →](/synapse-ai/solutions/general/agent-exec-storm-invisible-on-telegram)

---

## Quick Reference

| Error Code | Category | Most Common Cause |
|-----------|----------|-------------------|
| 401 | Auth | Cached credentials after key rotation |
| 429 | Rate Limit | No backoff config, burst requests |
| 529 | Rate Limit | Anthropic capacity, need model fallback |
| 1000 (WS) | Gateway | Handshake timeout on loaded gateway |
| `context_length_exceeded` | Context | Full history in every request |
| Silent channel drop | Gateway | Memory pressure or unhandled exception |

---

## Browse All OpenClaw Solutions

[← View all 253+ OpenClaw solutions](/synapse-ai/solutions/openclaw/)

Or search by error message on the [homepage](/) — results update in real time.

<div class="cta-box">
  <h3>Auto-fix OpenClaw errors</h3>
  <p>Install the SynapseAI skill to automatically search this database when your agent hits an error.</p>
  <code>clawhub install synapse-ai</code>
</div>
