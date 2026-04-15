---
layout: solution
title: "Agent Ignores Retry-After Header — Retries Too Soon and Gets 429 Again"
category: rate-limit
description: "API returns 429 with Retry-After: 60. Agent waits 1 second and retries. Gets 429 again. Retries immediately. Gets 429 again. Spends all its token budget retrying the same failed request, never actually waiting the required cooldown."
tags: [rate-limit, retry-after, 429, header, backoff, cooldown, exponential-backoff]
---

# Agent Ignores Retry-After Header — Retries Too Soon and Gets 429 Again

## Symptom
- Agent gets 429, retries after 1 second, gets 429 again, repeats 10 times
- Logs show: `429 Too Many Requests` followed immediately by another request
- `Retry-After: 60` header is present but ignored
- Agent exhausts its retry budget retrying too soon instead of waiting
- Each failed retry costs tokens (the tool call + error handling)
- Total wait time is much longer than if Retry-After had been respected from the start

## Root Cause
The `Retry-After` response header tells the client exactly how long to wait before retrying. It may be a number of seconds or an HTTP date. Many retry implementations ignore this header and use their own fixed or exponential backoff timing instead. When the server requires a specific cooldown (common for quota resets), ignoring Retry-After means the agent will keep retrying until the cooldown naturally expires — burning retry attempts along the way.

## Fix

### Option 1: Parse and respect Retry-After header

```python
import httpx
import asyncio
import time
from email.utils import parsedate_to_datetime

def parse_retry_after(retry_after_header: str) -> float:
    """
    Parse Retry-After header value.
    Accepts both: integer seconds ("60") and HTTP date ("Wed, 21 Oct 2025 07:28:00 GMT").
    Returns seconds to wait as a float.
    """
    if not retry_after_header:
        return 0.0

    # Try integer seconds first
    try:
        return float(retry_after_header.strip())
    except ValueError:
        pass

    # Try HTTP date format
    try:
        retry_time = parsedate_to_datetime(retry_after_header)
        wait = (retry_time.timestamp() - time.time())
        return max(0.0, wait)
    except Exception:
        pass

    return 60.0  # Safe fallback if parsing fails

async def request_with_retry_after(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    max_retries: int = 5,
    **kwargs
) -> httpx.Response:
    """
    Make HTTP request, respecting Retry-After header on 429 responses.
    """
    for attempt in range(max_retries):
        response = await client.request(method, url, **kwargs)

        if response.status_code != 429:
            return response

        if attempt == max_retries - 1:
            response.raise_for_status()

        # Parse Retry-After from response headers
        retry_after = parse_retry_after(
            response.headers.get("retry-after", "")
            or response.headers.get("x-ratelimit-reset-after", "")
            or response.headers.get("x-rate-limit-reset", "")
        )

        # Add small jitter to avoid synchronized retries
        import random
        jitter = random.uniform(0, min(retry_after * 0.1, 5.0))
        wait = retry_after + jitter

        print(
            f"Rate limited (attempt {attempt + 1}/{max_retries}). "
            f"Retry-After: {retry_after:.0f}s. "
            f"Waiting {wait:.1f}s (with {jitter:.1f}s jitter)..."
        )
        await asyncio.sleep(wait)

    raise RuntimeError(f"Max retries ({max_retries}) exceeded")

# Usage:
async with httpx.AsyncClient() as client:
    response = await request_with_retry_after(client, "POST", "/api/completions", json=payload)
```

### Option 2: Rate limit state tracker per API

```python
import time
import asyncio
from dataclasses import dataclass, field

@dataclass
class RateLimitState:
    """Track rate limit state for a specific API endpoint"""
    blocked_until: float = 0.0
    consecutive_429s: int = 0
    total_429s: int = 0
    last_429_at: float = 0.0

class RateLimitTracker:
    """
    Track rate limit state per API host.
    All requests to a rate-limited host wait automatically.
    """

    def __init__(self):
        self._states: dict[str, RateLimitState] = {}

    def _get_state(self, host: str) -> RateLimitState:
        if host not in self._states:
            self._states[host] = RateLimitState()
        return self._states[host]

    async def wait_if_blocked(self, host: str):
        """Wait until the rate limit window has passed"""
        state = self._get_state(host)
        now = time.monotonic()
        if state.blocked_until > now:
            wait = state.blocked_until - now
            print(f"Rate limit active for {host}. Waiting {wait:.1f}s...")
            await asyncio.sleep(wait)

    def record_429(self, host: str, retry_after_seconds: float):
        """Record a rate limit hit"""
        state = self._get_state(host)
        state.consecutive_429s += 1
        state.total_429s += 1
        state.last_429_at = time.monotonic()

        # Block this host until Retry-After expires
        state.blocked_until = time.monotonic() + retry_after_seconds
        print(
            f"Rate limit hit #{state.total_429s} for {host}. "
            f"Blocked for {retry_after_seconds:.0f}s "
            f"(until {time.strftime('%H:%M:%S', time.localtime(time.time() + retry_after_seconds))})"
        )

    def record_success(self, host: str):
        state = self._get_state(host)
        state.consecutive_429s = 0  # Reset on success

tracker = RateLimitTracker()

async def tracked_request(url: str, client: httpx.AsyncClient, **kwargs) -> httpx.Response:
    host = httpx.URL(url).host

    # Wait if this host is currently rate-limited
    await tracker.wait_if_blocked(host)

    response = await client.request("GET", url, **kwargs)

    if response.status_code == 429:
        retry_after = parse_retry_after(response.headers.get("retry-after", "60"))
        tracker.record_429(host, retry_after)
        # Retry after waiting
        await tracker.wait_if_blocked(host)
        response = await client.request("GET", url, **kwargs)

    if response.status_code < 400:
        tracker.record_success(host)

    return response
```

### Option 3: Extract rate limit info from response headers

```python
def extract_rate_limit_info(headers: dict) -> dict:
    """
    Extract rate limit metadata from common header formats.
    Covers: GitHub, Twitter, Stripe, Anthropic, OpenAI, etc.
    """
    info = {}

    # Standard Retry-After
    if "retry-after" in headers:
        info["retry_after_seconds"] = parse_retry_after(headers["retry-after"])

    # RateLimit-* (IETF draft standard)
    if "ratelimit-limit" in headers:
        info["limit"] = int(headers["ratelimit-limit"])
    if "ratelimit-remaining" in headers:
        info["remaining"] = int(headers["ratelimit-remaining"])
    if "ratelimit-reset" in headers:
        info["resets_at"] = float(headers["ratelimit-reset"])
        info["resets_in"] = max(0, info["resets_at"] - time.time())

    # X-RateLimit-* (common variant)
    for variant in ("x-ratelimit-limit", "x-rate-limit-limit"):
        if variant in headers:
            info["limit"] = int(headers[variant])
    for variant in ("x-ratelimit-remaining", "x-rate-limit-remaining"):
        if variant in headers:
            info["remaining"] = int(headers[variant])
    for variant in ("x-ratelimit-reset", "x-rate-limit-reset"):
        if variant in headers:
            reset_val = headers[variant]
            try:
                info["resets_at"] = float(reset_val)
                info["resets_in"] = max(0, info["resets_at"] - time.time())
            except ValueError:
                info["resets_in"] = parse_retry_after(reset_val)

    # Anthropic-specific
    if "anthropic-ratelimit-requests-remaining" in headers:
        info["requests_remaining"] = int(headers["anthropic-ratelimit-requests-remaining"])
    if "anthropic-ratelimit-tokens-remaining" in headers:
        info["tokens_remaining"] = int(headers["anthropic-ratelimit-tokens-remaining"])

    return info

# Log rate limit state on every response:
async def logged_request(url: str, client: httpx.AsyncClient, **kwargs) -> httpx.Response:
    response = await client.request("GET", url, **kwargs)
    rl_info = extract_rate_limit_info(dict(response.headers))
    if rl_info:
        remaining = rl_info.get("remaining", "?")
        resets_in = rl_info.get("resets_in")
        if isinstance(resets_in, float):
            print(f"Rate limit: {remaining} remaining, resets in {resets_in:.0f}s")
    return response
```

### Option 4: Proactive rate limit management

```python
import asyncio

class ProactiveRateLimiter:
    """
    Slow down requests as remaining quota approaches zero.
    Prevents hitting the rate limit in the first place.
    """

    def __init__(self, target_remaining_buffer: int = 10):
        self.remaining = None
        self.limit = None
        self.resets_in = None
        self.buffer = target_remaining_buffer
        self._lock = asyncio.Lock()

    def update_from_response(self, headers: dict):
        """Update state from response headers"""
        info = extract_rate_limit_info(headers)
        if "remaining" in info:
            self.remaining = info["remaining"]
        if "limit" in info:
            self.limit = info["limit"]
        if "resets_in" in info:
            self.resets_in = info["resets_in"]

    async def throttle(self):
        """Slow down if remaining quota is low"""
        async with self._lock:
            if self.remaining is None:
                return  # No rate limit info yet — proceed

            if self.remaining <= 0:
                # Out of quota — wait for reset
                wait = self.resets_in or 60.0
                print(f"Quota exhausted. Waiting {wait:.0f}s for reset...")
                await asyncio.sleep(wait)

            elif self.remaining <= self.buffer:
                # Getting close to limit — add delay proportional to usage
                pct_used = 1 - (self.remaining / (self.limit or 100))
                delay = pct_used * 2.0  # Up to 2s delay as quota runs low
                print(f"Quota low ({self.remaining} remaining) — throttling by {delay:.1f}s")
                await asyncio.sleep(delay)

limiter = ProactiveRateLimiter(target_remaining_buffer=20)

async def throttled_api_call(payload: dict, client: httpx.AsyncClient) -> dict:
    await limiter.throttle()  # Slow down if needed
    response = await client.post("/api/completions", json=payload)
    limiter.update_from_response(dict(response.headers))
    response.raise_for_status()
    return response.json()
```

### Option 5: System prompt awareness of rate limits

```
System prompt:
"Rate limit handling rules:

1. When you receive a 429 Too Many Requests error:
   a. Check the response for a 'Retry-After' header
   b. Wait EXACTLY that many seconds before retrying
   c. Do NOT retry before the Retry-After period has elapsed

2. If no Retry-After header is present, wait at least 60 seconds before retrying.

3. After 3 consecutive 429 errors, stop retrying and report:
   'API rate limit reached. Retry-After: {N} seconds. Please try again later.'

4. Do NOT retry immediately after a 429 — this makes the situation worse.

5. Each retry attempt costs tokens. If the rate limit is long (> 5 minutes),
   report the limitation instead of waiting silently."
```

## Retry-After Header Formats

| Format | Example | Meaning |
|---|---|---|
| Integer seconds | `Retry-After: 60` | Wait 60 seconds |
| HTTP date | `Retry-After: Wed, 21 Oct 2025 07:28:00 GMT` | Wait until this timestamp |
| `X-RateLimit-Reset` | `X-RateLimit-Reset: 1729499280` | Unix timestamp of quota reset |
| `X-RateLimit-Reset-After` | `X-RateLimit-Reset-After: 45.2` | Seconds until quota reset |
| Anthropic | `anthropic-ratelimit-requests-reset: 2025-10-21T07:28:00Z` | ISO timestamp |

## Expected Token Savings
10 retries × 500 tokens each without respecting Retry-After: ~5,000 wasted tokens
Respecting Retry-After: 1 wait + 1 successful retry = ~500 tokens

## Environment
- Any agent calling rate-limited APIs; critical for OpenAI, Anthropic, GitHub, Stripe, and all quota-based services
- Source: direct experience; Retry-After violations are the most common cause of extended 429 storms
