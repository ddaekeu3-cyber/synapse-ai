---
layout: default
title: "Rate Limit 429 Errors in AI Agents — Causes, Fixes, and Prevention"
description: "Complete guide to fixing rate limit 429 errors in OpenClaw and AI agents. Exponential backoff, model fallback, request queuing, and cost impact explained."
permalink: /guide/rate-limit-errors
---

# Rate Limit 429 Errors in AI Agents

Rate limit errors are the most common source of unexpected token waste in OpenClaw. An agent that doesn't handle 429s correctly will retry in a tight loop, burning 10–20x more tokens than the original task required.

---

## Why 429 Errors Are Expensive

A standard agent hitting a rate limit without proper handling:

1. Receives `429 Too Many Requests`
2. Retries immediately → another 429
3. Retries again → another 429
4. Logs a failure and tries a different approach
5. That approach also hits the rate limit
6. **Loop continues for 5–15 attempts before giving up**

**Cost:** 10,000–30,000 tokens ($3–9) for what should have been a 1-second wait.

**With exponential backoff:** 500 tokens and ~60 seconds of wait time.

---

## Provider Rate Limits Reference

| Provider | Limit Type | Default Tier | Notes |
|----------|-----------|--------------|-------|
| Anthropic Claude | RPM + TPM | Tier 1: 50 RPM | Increases with spend history |
| OpenAI GPT | RPM + TPM | Tier 1: 500 RPM | Resets per minute |
| Google Gemini | RPM | Free: 15 RPM | Generous paid tier |
| Anthropic (529) | Capacity | Varies | Server overload, not account limit |

---

## Fix 1: Exponential Backoff with Jitter (Required)

The minimum fix. Without this, every retry makes the rate limit problem worse.

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    retry:
      max_attempts: 5
      initial_delay_ms: 1000
      backoff_multiplier: 2.0
      jitter: true          # Prevents thundering herd
      on_status_codes: [429, 529, 503]
```

**Why jitter matters:** Without jitter, all retrying agents hit the API at the same backoff intervals, creating a new burst that triggers another 429.

---

## Fix 2: Request Queuing

For batch operations, queue requests and enforce rate limits client-side before hitting the API:

```javascript
// Rate limiter: 40 requests/minute (10% below API limit as buffer)
const limiter = new RateLimiter({ rpm: 40 });

for (const task of tasks) {
  await limiter.throttle();
  const result = await api.call(task);
}
```

This prevents hitting the limit in the first place. **Token savings: up to 90%** on batch operations.

---

## Fix 3: Model Fallback for 529 (Capacity) Errors

Error 529 means the specific model is overloaded, not your account limit. Switch to an available model:

```yaml
providers:
  anthropic:
    primary_model: claude-opus-4-6
    fallback_model: claude-sonnet-4-6
    fallback_on: [529, 503]
    fallback_timeout_ms: 5000
```

Sonnet is almost always available when Opus is overloaded. Task quality degrades slightly but the task completes.

---

## Fix 4: Respect Retry-After Headers

The API tells you exactly how long to wait. Use it:

```python
import time

def call_with_retry(fn, max_attempts=5):
    for attempt in range(max_attempts):
        response = fn()
        if response.status_code == 429:
            retry_after = int(response.headers.get('retry-after', 60))
            time.sleep(retry_after + 1)  # +1s buffer
            continue
        return response
    raise Exception("Max retries exceeded")
```

This is more accurate than fixed backoff for providers that return `Retry-After`.

---

## Fix 5: Token Budget Limits

Prevent rate limit storms by capping token usage per session:

```yaml
# openclaw.config.yaml
limits:
  max_tokens_per_session: 50000
  max_tokens_per_task: 10000
  max_api_calls_per_minute: 30
```

When the limit is reached, the agent pauses and reports status instead of continuing to burn tokens.

---

## Diagnosing Your Rate Limit Pattern

### Is it RPM (requests per minute) or TPM (tokens per minute)?

- **RPM:** 429 appears after N requests regardless of size → reduce request frequency
- **TPM:** 429 appears after large responses or batch completions → reduce per-request context size

```bash
# Check which limit you're hitting
openclaw logs --filter "429" --last 1h | grep -E "rpm|tpm|rate_limit_type"
```

### Is it a burst or sustained problem?

- **Burst:** 429s appear in clusters then resolve → add jitter to backoff
- **Sustained:** 429s appear consistently → you've exceeded your tier limit, need tier upgrade or request batching

---

## Telegram-Specific: 429 + SIGTERM Loop

A common pattern in OpenClaw + Telegram setups:

1. Telegram API returns 429 (too many messages)
2. OpenClaw retries immediately
3. System sends SIGTERM to the gateway after repeated failures
4. Gateway restarts and loses session state
5. New session starts fresh and hammers Telegram API again → loop

**Fix:** Add message queue with rate limiting before Telegram channel:

```yaml
channels:
  telegram:
    rate_limit:
      messages_per_second: 1
      burst_limit: 10
    queue:
      enabled: true
      max_size: 100
```

[Full Telegram 429 solution →](/synapse-ai/solutions/telegram/)

---

## Prevention Checklist

Before deploying any agent that makes external API calls:

- [ ] Exponential backoff configured with jitter
- [ ] `Retry-After` header parsing enabled
- [ ] Model fallback configured for 529/503
- [ ] Per-session and per-task token limits set
- [ ] Request queuing for any batch operations
- [ ] Rate limit metrics monitored (alerts on >10 429s/hour)

---

## Browse Rate Limit Solutions

[← View all rate limit solutions](/synapse-ai/solutions/rate-limit/)

<div class="cta-box">
  <h3>Auto-detect rate limit patterns</h3>
  <p>SynapseAI monitors your agent's 429 patterns and suggests the right fix based on your specific provider and usage profile.</p>
  <code>clawhub install synapse-ai</code>
</div>
