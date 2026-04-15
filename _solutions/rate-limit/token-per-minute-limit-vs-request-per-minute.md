---
layout: solution
title: "Hitting Token-Per-Minute Limit Instead of Request-Per-Minute — Different Rate Limits Confused"
category: rate-limit
description: "Agent gets 429s even with slow request rate. Problem is tokens-per-minute (TPM), not requests-per-minute (RPM). Sending fewer requests doesn't help if each request is large."
tags: [rate-limit, tpm, rpm, tokens, 429, anthropic-limits]
---

# Hitting Token-Per-Minute Limit Instead of Request-Per-Minute — Different Rate Limits Confused

## Symptom
- Getting 429 errors even with only 1-2 requests per minute
- Reducing request frequency doesn't stop the 429s
- Large context requests fail; small ones succeed
- Error message mentions `tokens` not `requests`
- 429s occur in bursts, then clear for a minute

## Root Cause
Anthropic enforces multiple independent rate limits simultaneously:
- **RPM** (requests per minute) — number of API calls
- **TPM** (tokens per minute) — total input + output tokens across all calls
- **OTPM** (output tokens per minute) — output tokens specifically (some tiers)

A single request with 100K input tokens consumes the entire TPM budget for a minute at lower tiers, even if it's the only request made.

## Anthropic Rate Limits by Tier (as of 2025)

| Tier | RPM | TPM | Notes |
|---|---|---|---|
| Free | 5 | 25,000 | Very restricted |
| Build (Tier 1) | 50 | 50,000 | $5 deposit |
| Build (Tier 2) | 1,000 | 160,000 | $40 spend |
| Scale (Tier 3) | 2,000 | 320,000 | $200 spend |
| Scale (Tier 4) | 4,000 | 400,000 | $400 spend |

Check current limits: https://docs.anthropic.com/en/api/rate-limits

## Fix

### Option 1: Track token usage, not just request count

```python
import time
from collections import deque

class TokenRateLimiter:
    def __init__(self, tpm_limit: int, rpm_limit: int):
        self.tpm_limit = tpm_limit
        self.rpm_limit = rpm_limit
        self.token_usage = deque()  # (timestamp, tokens) pairs
        self.request_times = deque()  # timestamps

    def _cleanup_old_entries(self):
        now = time.time()
        minute_ago = now - 60
        while self.token_usage and self.token_usage[0][0] < minute_ago:
            self.token_usage.popleft()
        while self.request_times and self.request_times[0] < minute_ago:
            self.request_times.popleft()

    def tokens_used_last_minute(self) -> int:
        self._cleanup_old_entries()
        return sum(tokens for _, tokens in self.token_usage)

    def requests_last_minute(self) -> int:
        self._cleanup_old_entries()
        return len(self.request_times)

    async def wait_if_needed(self, estimated_tokens: int):
        self._cleanup_old_entries()
        tokens_used = self.tokens_used_last_minute()

        if tokens_used + estimated_tokens > self.tpm_limit * 0.9:
            wait_time = 60 - (time.time() - self.token_usage[0][0])
            print(f"TPM limit approaching ({tokens_used}/{self.tpm_limit}). Waiting {wait_time:.1f}s")
            await asyncio.sleep(max(0, wait_time))

        if self.requests_last_minute() >= self.rpm_limit * 0.9:
            wait_time = 60 - (time.time() - self.request_times[0])
            print(f"RPM limit approaching. Waiting {wait_time:.1f}s")
            await asyncio.sleep(max(0, wait_time))

    def record_usage(self, tokens_used: int):
        now = time.time()
        self.token_usage.append((now, tokens_used))
        self.request_times.append(now)

limiter = TokenRateLimiter(tpm_limit=160_000, rpm_limit=1000)
```

### Option 2: Estimate tokens before calling

```python
def estimate_tokens(messages: list[dict], model: str = "claude-sonnet-4-6") -> int:
    """Rough token estimate: ~4 chars per token"""
    total_chars = sum(
        len(str(m.get("content", "")))
        for m in messages
    )
    return total_chars // 4

async def call_with_tpm_awareness(messages: list[dict], max_tokens: int = 1024):
    estimated_input = estimate_tokens(messages)
    estimated_total = estimated_input + max_tokens  # Input + max possible output

    await limiter.wait_if_needed(estimated_total)

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=messages
    )

    actual_tokens = response.usage.input_tokens + response.usage.output_tokens
    limiter.record_usage(actual_tokens)
    return response
```

### Option 3: Reduce input tokens to stay under TPM

```python
def truncate_context_to_token_budget(
    messages: list[dict],
    max_input_tokens: int = 40_000  # Leave headroom for TPM
) -> list[dict]:
    """Trim context to fit within token budget"""
    total = estimate_tokens(messages)
    if total <= max_input_tokens:
        return messages

    # Keep system message + last N user/assistant turns
    trimmed = []
    budget = max_input_tokens

    # Always keep first (system) message
    if messages and messages[0].get("role") == "system":
        trimmed = [messages[0]]
        budget -= estimate_tokens([messages[0]])
        messages = messages[1:]

    # Add recent messages from the end
    for msg in reversed(messages):
        msg_tokens = estimate_tokens([msg])
        if budget - msg_tokens < 0:
            break
        trimmed.insert(1 if trimmed else 0, msg)
        budget -= msg_tokens

    return trimmed
```

### Option 4: Read actual limit from 429 response headers

```python
import anthropic, time

def handle_rate_limit(error: anthropic.RateLimitError) -> float:
    """Extract wait time from rate limit error"""
    headers = getattr(error, 'response', None) and error.response.headers

    if headers:
        # Anthropic returns retry-after in seconds
        retry_after = headers.get('retry-after') or headers.get('x-ratelimit-reset-tokens')
        if retry_after:
            return float(retry_after)

    # Default: wait 60 seconds (one full minute window reset)
    return 60.0

async def resilient_completion(messages, **kwargs):
    for attempt in range(5):
        try:
            return await client.messages.create(messages=messages, **kwargs)
        except anthropic.RateLimitError as e:
            wait = handle_rate_limit(e)
            print(f"Rate limited (attempt {attempt+1}). Waiting {wait:.1f}s. Error: {e}")
            await asyncio.sleep(wait)
    raise RuntimeError("Exceeded rate limit retry budget")
```

## Diagnosing Which Limit You're Hitting

```python
# Check response headers for current limit state
response = client.messages.create(...)

# Anthropic rate limit headers:
# x-ratelimit-limit-requests      — your RPM limit
# x-ratelimit-limit-tokens        — your TPM limit
# x-ratelimit-remaining-requests  — RPM remaining this minute
# x-ratelimit-remaining-tokens    — TPM remaining this minute
# x-ratelimit-reset-requests      — when RPM resets
# x-ratelimit-reset-tokens        — when TPM resets

# Log these to understand which limit you're approaching:
print(response.headers.get('x-ratelimit-remaining-tokens'))
print(response.headers.get('x-ratelimit-remaining-requests'))
```

## Expected Token Savings
Not applicable — this is about avoiding 429 errors, not reducing token usage.

## Environment
- High-volume agents; most critical when using large contexts or many parallel requests
- Source: Anthropic rate limit documentation, direct experience at scale
