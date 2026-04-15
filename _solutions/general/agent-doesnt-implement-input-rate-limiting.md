---
layout: solution
title: "Agent Doesn't Implement Input Rate Limiting"
category: general
description: "A single user or abusive client can flood the agent with hundreds of requests per second, running up API costs and starving other users of capacity."
tags: [general, security, rate-limiting, abuse-prevention, cost-control, reliability]
---

## Symptom

A user (or a bot) submits 500 messages in 60 seconds, each triggering a full LLM call. API costs spike unexpectedly. Legitimate users experience degraded response times as all concurrency slots are consumed. A script-kiddie discovers the agent endpoint and uses it as a free LLM proxy. The month's API budget is exhausted in an hour.

## Root Cause

Agent endpoints that accept user messages without rate limiting are open to two failure modes: accidental (a client bug retrying in a tight loop) and deliberate (cost-amplification abuse). Without a per-user or per-IP request counter, there is nothing to stop any single client from consuming the entire capacity of the agent.

## Fix

### Option 1 — In-memory sliding window rate limiter per user

```python
import time
import collections
import anthropic

client = anthropic.Anthropic()

class SlidingWindowRateLimiter:
    """Per-user sliding window rate limiter. Thread-safe for single-process use."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window       = window_seconds
        self._windows: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def is_allowed(self, user_id: str) -> tuple[bool, dict]:
        """Returns (allowed, metadata). Registers the request if allowed."""
        now    = time.monotonic()
        dq     = self._windows[user_id]
        cutoff = now - self.window

        # Evict expired events
        while dq and dq[0] < cutoff:
            dq.popleft()

        count = len(dq)
        if count >= self.max_requests:
            oldest    = dq[0]
            retry_after = round(oldest + self.window - now + 0.1, 1)
            return False, {
                "allowed":      False,
                "requests":     count,
                "limit":        self.max_requests,
                "window":       self.window,
                "retry_after":  retry_after,
            }

        dq.append(now)
        return True, {
            "allowed":    True,
            "requests":   count + 1,
            "limit":      self.max_requests,
            "remaining":  self.max_requests - count - 1,
        }

# 5 requests per 10 seconds per user
limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=10)

def ask(user_id: str, message: str) -> str:
    allowed, meta = limiter.is_allowed(user_id)
    if not allowed:
        return (
            f"Rate limit exceeded. You can send {meta['limit']} messages "
            f"per {meta['window']}s. Retry after {meta['retry_after']}s."
        )

    print(f"  [{user_id}] {meta['requests']}/{meta['limit']} requests in window")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text

# Simulate normal user and abusive user
print("Normal user (5 requests spread out):")
for i in range(5):
    reply = ask("alice", f"Question {i+1}: what is {i}+{i}?")
    print(f"  Q{i+1}: {reply.strip()[:50]}")

print("\nAbusive user (8 rapid requests):")
for i in range(8):
    reply = ask("attacker", f"Flood request {i+1}")
    print(f"  R{i+1}: {reply[:60]}")
```

**Expected Token Savings:** Each blocked request saves the full cost of an LLM call; at 5 req/10s limit, an attacker sending 500 req/min is blocked after the 5th request, preventing 495 wasted API calls per minute.
**Environment:** All public-facing agents; per-user rate limiting is a mandatory cost-control and fairness mechanism.

---

### Option 2 — Token bucket rate limiter with burst allowance

```python
import time
import threading
import anthropic

client = anthropic.Anthropic()

class TokenBucketLimiter:
    """
    Token bucket: allows short bursts up to bucket capacity,
    then enforces a sustained rate limit.
    """
    def __init__(self, rate: float, capacity: int):
        """
        rate:     tokens added per second (sustained request rate)
        capacity: maximum tokens in bucket (burst allowance)
        """
        self.rate     = rate
        self.capacity = capacity
        self._buckets: dict[str, dict] = {}
        self._lock    = threading.Lock()

    def _get_bucket(self, user_id: str) -> dict:
        if user_id not in self._buckets:
            self._buckets[user_id] = {"tokens": self.capacity, "last_refill": time.monotonic()}
        return self._buckets[user_id]

    def consume(self, user_id: str, tokens: int = 1) -> tuple[bool, float]:
        """Returns (allowed, wait_seconds_if_denied)."""
        with self._lock:
            now    = time.monotonic()
            bucket = self._get_bucket(user_id)

            # Refill tokens based on elapsed time
            elapsed           = now - bucket["last_refill"]
            bucket["tokens"]  = min(self.capacity, bucket["tokens"] + elapsed * self.rate)
            bucket["last_refill"] = now

            if bucket["tokens"] >= tokens:
                bucket["tokens"] -= tokens
                return True, 0.0
            else:
                wait = (tokens - bucket["tokens"]) / self.rate
                return False, round(wait, 1)

# Sustained rate: 2 req/s, burst allowance: 5 requests
limiter = TokenBucketLimiter(rate=2.0, capacity=5)

def ask(user_id: str, message: str) -> str:
    allowed, wait = limiter.consume(user_id)
    if not allowed:
        return f"Rate limit: please wait {wait}s before your next message."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text.strip()

# Demonstrate burst allowance then rate enforcement
print("Burst of 5 requests (all should succeed):")
for i in range(5):
    reply = ask("bob", f"Burst request {i+1}")
    print(f"  [{i+1}] {reply[:50]}")

print("\n6th request (should be rate-limited):")
reply = ask("bob", "One more")
print(f"  [6] {reply[:100]}")

time.sleep(1.0)
print("\nAfter 1s cooldown (should succeed):")
reply = ask("bob", "After cooldown")
print(f"  [7] {reply[:50]}")
```

**Expected Token Savings:** Token bucket allows legitimate burst usage (e.g., user pastes 5 questions at once) while preventing sustained abuse; more user-friendly than rigid sliding window for interactive sessions.
**Environment:** Interactive chat agents where users legitimately send a few messages in quick succession; token bucket balances abuse prevention with good UX.

---

### Option 3 — Tiered rate limits by user plan

```python
import time
import collections
import anthropic

client = anthropic.Anthropic()

# Rate limits per plan tier (requests per minute)
PLAN_LIMITS = {
    "free":       {"rpm": 5,   "daily": 50},
    "pro":        {"rpm": 30,  "daily": 1000},
    "enterprise": {"rpm": 200, "daily": 50000},
}

class TieredRateLimiter:
    def __init__(self):
        self._minute_windows: dict[str, collections.deque]     = collections.defaultdict(collections.deque)
        self._daily_counts:   dict[str, tuple[int, float]]     = {}   # user_id → (count, day_start)

    def _day_start(self) -> float:
        """Start of the current calendar day (UTC) as monotonic offset."""
        import datetime
        now = datetime.datetime.utcnow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return time.monotonic() - (now - start).total_seconds()

    def check(self, user_id: str, plan: str) -> tuple[bool, str]:
        limits   = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        rpm      = limits["rpm"]
        daily    = limits["daily"]
        now      = time.monotonic()

        # Per-minute check (sliding window)
        dq     = self._minute_windows[user_id]
        cutoff = now - 60.0
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= rpm:
            return False, f"Rate limit: {rpm} requests/minute for {plan} plan. Retry in {round(dq[0]+60-now+0.1,1)}s."

        # Daily check
        count, day_start = self._daily_counts.get(user_id, (0, self._day_start()))
        if now > day_start + 86400:
            count, day_start = 0, self._day_start()
        if count >= daily:
            return False, f"Daily limit: {daily} requests/day for {plan} plan. Resets at midnight UTC."

        # Record the request
        dq.append(now)
        self._daily_counts[user_id] = (count + 1, day_start)
        return True, f"OK ({len(dq)}/{rpm} rpm, {count+1}/{daily} today)"

limiter = TieredRateLimiter()

def ask(user_id: str, plan: str, message: str) -> str:
    allowed, status = limiter.check(user_id, plan)
    print(f"  [{user_id}/{plan}] {status}")
    if not allowed:
        return f"⛔ {status}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text.strip()

# Test different plan tiers
for user, plan, msg in [
    ("alice", "enterprise", "Analyse our Q1 data."),
    ("bob",   "pro",        "Summarise this document."),
    ("carol", "free",       "What is Python?"),
    ("carol", "free",       "What is Java?"),
    ("carol", "free",       "What is Go?"),
    ("carol", "free",       "What is Rust?"),
    ("carol", "free",       "What is C++?"),
    ("carol", "free",       "What is Ruby?"),   # should be rate-limited
]:
    reply = ask(user, plan, msg)
    print(f"    Reply: {reply[:60]}\n")
```

**Expected Token Savings:** Tiered limits monetise the rate limiting structure; free users are capped at low cost, preventing the business model from being undermined by unlimited free usage.
**Environment:** SaaS agents with free/paid tiers; tiered limits are both a cost control and a conversion mechanism.

---

### Option 4 — Async rate limiter for FastAPI / asyncio services

```python
import asyncio
import time
import collections
import anthropic
from typing import Callable

client = anthropic.AsyncAnthropic()

class AsyncRateLimiter:
    """Async-safe sliding window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window       = window_seconds
        self._windows: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = asyncio.Lock()

    async def is_allowed(self, user_id: str) -> tuple[bool, dict]:
        async with self._lock:
            now    = time.monotonic()
            dq     = self._windows[user_id]
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                return False, {"retry_after": round(dq[0] + self.window - now + 0.1, 1)}
            dq.append(now)
            return True, {"remaining": self.max_requests - len(dq)}

limiter = AsyncRateLimiter(max_requests=3, window_seconds=5.0)

async def ask(user_id: str, message: str) -> str:
    allowed, meta = await limiter.is_allowed(user_id)
    if not allowed:
        return f"Rate limited. Retry after {meta['retry_after']}s."
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text.strip()

async def simulate_concurrent_users() -> None:
    # Simulate concurrent requests from two users
    tasks = [
        ask("user-a", f"Message {i} from user A")
        for i in range(5)
    ] + [
        ask("user-b", f"Message {i} from user B")
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    a_results = results[:5]
    b_results = results[5:]
    print("User A:", [str(r)[:40] for r in a_results])
    print("User B:", [str(r)[:40] for r in b_results])

asyncio.run(simulate_concurrent_users())
```

**Expected Token Savings:** Async limiter uses a lock to prevent race conditions where concurrent requests all pass the check simultaneously; prevents the "thundering herd" bypass where many concurrent requests all see count=0.
**Environment:** FastAPI or asyncio-based agents serving concurrent users; async lock is essential for correctness in concurrent environments.

---

### Option 5 — Cost-aware rate limiting: track token spend per user

```python
import time
import collections
import anthropic

client = anthropic.Anthropic()

class CostAwareRateLimiter:
    """
    Limits users by token spend per window, not just request count.
    Prevents a user from sending one huge request that costs as much as 100 small ones.
    """
    def __init__(self, max_tokens_per_window: int, window_seconds: int):
        self.max_tokens = max_tokens_per_window
        self.window     = window_seconds
        self._events: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        # events: deque of (timestamp, token_count)

    def tokens_in_window(self, user_id: str) -> int:
        now    = time.monotonic()
        dq     = self._events[user_id]
        cutoff = now - self.window
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        return sum(t for _, t in dq)

    def can_afford(self, user_id: str, estimated_tokens: int) -> bool:
        return self.tokens_in_window(user_id) + estimated_tokens <= self.max_tokens

    def record(self, user_id: str, actual_tokens: int) -> None:
        self._events[user_id].append((time.monotonic(), actual_tokens))

# Max 10,000 tokens per user per minute
limiter = CostAwareRateLimiter(max_tokens_per_window=10_000, window_seconds=60)

def ask(user_id: str, message: str) -> str:
    estimated = len(message.split()) * 2 + 200   # rough token estimate
    if not limiter.can_afford(user_id, estimated):
        used = limiter.tokens_in_window(user_id)
        return (
            f"Token budget exceeded ({used:,}/{limiter.max_tokens:,} tokens used this minute). "
            f"Please wait before sending more messages."
        )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    actual = response.usage.input_tokens + response.usage.output_tokens
    limiter.record(user_id, actual)
    print(f"  [{user_id}] {actual} tokens used | budget: {limiter.tokens_in_window(user_id):,}/{limiter.max_tokens:,}")
    return response.content[0].text.strip()

# Normal queries
for msg in ["What is Python?", "What is a decorator?"]:
    ask("alice", msg)

# Long input that burns tokens
long_msg = "Summarise this: " + "word " * 500
reply = ask("alice", long_msg)
print(f"Long query: {reply[:60]}")
```

**Expected Token Savings:** Token-based limits prevent a single expensive request from consuming the budget of 100 cheap ones; fairer than request-count limits for mixed workloads.
**Environment:** Agents with variable-length inputs (document summarisation, code analysis) where request count is a poor proxy for actual API cost.

---

### Option 6 — IP and user-agent fingerprinting for bot detection

```python
import hashlib
import time
import collections
import re
import anthropic

client = anthropic.Anthropic()

SUSPICIOUS_USER_AGENTS = {
    "python-requests", "curl", "wget", "httpie", "postman",
    "scrapy", "selenium", "playwright", "puppeteer",
}

class BotDetector:
    """Detects and rate-limits likely bot traffic."""

    def __init__(self):
        self._ip_windows:  dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._flagged_ips: dict[str, float] = {}   # ip → flagged_until timestamp

    def fingerprint(self, ip: str, user_agent: str) -> str:
        raw = f"{ip}|{user_agent.lower()[:50]}"
        return hashlib.md5(raw.encode()).hexdigest()[:8]

    def is_suspicious_ua(self, user_agent: str) -> bool:
        ua_lower = user_agent.lower()
        return any(bot in ua_lower for bot in SUSPICIOUS_USER_AGENTS)

    def check(self, ip: str, user_agent: str) -> tuple[bool, str]:
        now = time.monotonic()

        # Check if IP is flagged
        flagged_until = self._flagged_ips.get(ip, 0)
        if now < flagged_until:
            return False, f"Access suspended. Retry after {round(flagged_until - now)}s."

        # Suspicious user-agent → extra strict limit
        if self.is_suspicious_ua(user_agent):
            return False, "Automated clients are not permitted. Use a browser or our official SDK."

        # Rapid-request detection: >20 requests in 10 seconds → flag for 5 minutes
        dq = self._ip_windows[ip]
        while dq and dq[0] < now - 10:
            dq.popleft()
        dq.append(now)

        if len(dq) > 20:
            self._flagged_ips[ip] = now + 300   # flag for 5 minutes
            return False, "Too many requests. Access suspended for 5 minutes."

        return True, f"OK ({len(dq)} requests in last 10s)"

detector = BotDetector()

def ask(ip: str, user_agent: str, message: str) -> str:
    allowed, status = detector.check(ip, user_agent)
    if not allowed:
        return f"⛔ {status}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text.strip()

# Legitimate browser user
print("Browser user:")
print(ask("1.2.3.4", "Mozilla/5.0 (Macintosh; Intel Mac OS X)", "What is Python?"))

# Suspicious user agent
print("\nBot user agent:")
print(ask("5.6.7.8", "python-requests/2.28.0", "What is Python?"))

# Rapid requests from one IP
print("\nRapid request flood:")
for i in range(5):
    reply = ask("9.10.11.12", "Mozilla/5.0", f"Request {i}")
    print(f"  [{i}] {reply[:40]}")
```

**Expected Token Savings:** Bot detection blocks automated abuse before it reaches the LLM; particularly effective against script-based attacks that use obvious automation fingerprints.
**Environment:** Public-facing agents without authentication; bot detection adds a low-friction layer that stops the most common automated abuse patterns.

---

## Comparison

| Option | Scope | Burst Support | Cost-Aware | Abuse Detection | Best For |
|---|---|---|---|---|---|
| 1. Sliding window | Per-user RPM | No | No | No | Simple baseline rate limiting |
| 2. Token bucket | Per-user with burst | Yes | No | No | Interactive chat with natural bursts |
| 3. Tiered limits | Per-user + plan | No | No | No | SaaS with free/paid tiers |
| 4. Async limiter | Per-user (async-safe) | No | No | No | FastAPI / asyncio services |
| 5. Cost-aware (tokens) | Per-user by spend | No | Yes | No | Variable-length input agents |
| 6. Bot detection | Per-IP + UA | No | No | Yes | Public endpoints without auth |
